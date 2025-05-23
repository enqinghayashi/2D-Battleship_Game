"""
server.py

Serves a single-player Battleship session to one connected client.
Game logic is handled entirely on the server using battleship.py.
Client sends FIRE commands, and receives game feedback.

TODO: For Tier 1, item 1, you don't need to modify this file much. 
The core issue is in how the client handles incoming messages.
However, if you want to support multiple clients (i.e. progress through further Tiers), you'll need concurrency here too.
"""

import socket
import threading
import time
import queue
import struct
import select  # Add this import
from battleship import run_single_player_game_online, run_two_player_game_online, Board, BOARD_SIZE, SHIPS
from protocol import build_packet, parse_packet, PKT_TYPE_GAME, PKT_TYPE_CHAT

HOST = '127.0.0.1'
PORT = 5000

waiting_lines = []
waiting_players_lock = threading.Lock() # a lock to thread for needing 2 players to start the game
game_lock = threading.Lock()
game_running = threading.Event()

# Add player_sessions to track username -> session info
player_sessions = {}  # username: { 'conn': ..., 'addr': ..., 'game': ..., 'last_active': ..., 'reconnect_token': ..., ... }
player_sessions_lock = threading.Lock()

RECONNECT_TIMEOUT = 60  # seconds

# --- NEW: Persistent game state storage ---
games = {}  # (username1, username2): { 'board1': ..., 'board2': ..., 'turn': ..., 'ships1': ..., 'ships2': ..., 'placed1': ..., 'placed2': ... }

active_connections = []
active_connections_lock = threading.Lock()

# Add single player game states dictionary
single_player_games = {}  # username: {'board': board, 'ships_placed': bool, 'game_started': bool}
single_player_games_lock = threading.Lock()

def send_packet(conn, seq, pkt_type, msg):
    """Send a packet with the given sequence, type, and string payload."""
    payload = msg.encode('utf-8')
    packet = build_packet(seq, pkt_type, payload)
    conn.sendall(packet)

def recv_packet(conn):
    """Receive a packet and return (seq, pkt_type, payload as str)."""
    # Read header first to get payload length
    header_size = 7  # 4+1+2
    header = b''
    while len(header) < header_size:
        chunk = conn.recv(header_size - len(header))
        if not chunk:
            raise ConnectionError("Client disconnected")
        header += chunk
    seq, pkt_type, length = struct.unpack("!IBH", header)
    payload = b''
    while len(payload) < length:
        chunk = conn.recv(length - len(payload))
        if not chunk:
            raise ConnectionError("Client disconnected")
        payload += chunk
    checksum = b''
    while len(checksum) < 4:
        chunk = conn.recv(4 - len(checksum))
        if not chunk:
            raise ConnectionError("Client disconnected")
        checksum += chunk
    packet = header + payload + checksum
    try:
        seq, pkt_type, payload = parse_packet(packet)
        return seq, pkt_type, payload.decode('utf-8')
    except Exception as e:
        # Optionally log or handle checksum error
        return None, None, None

def handle_initial_connection(conn, addr):
    """
    Handles the initial handshake to get the username.
    Returns (username, conn, addr) or (None, None, None) on failure.
    """
    try:
        seq = 0
        seq_recv = 0
        # Receive USERNAME packet
        seq_recv, pkt_type, payload = recv_packet(conn)
        if pkt_type != PKT_TYPE_GAME or not payload.startswith("USERNAME "):
            send_packet(conn, seq, PKT_TYPE_GAME, "ERROR: Must provide USERNAME <name> as first message.")
            conn.close()
            return None, None, None
        username = payload.strip().split(" ", 1)[1]
        if not username:
            send_packet(conn, seq, PKT_TYPE_GAME, "ERROR: Username cannot be empty.")
            conn.close()
            return None, None, None
        print(f"[EVENT] Received username: {username} from {addr}")
        # --- Send a protocol welcome/lobby message immediately after handshake ---
        send_packet(conn, seq+1, PKT_TYPE_GAME, "WELCOME! Waiting for game to start...")
        return username, conn, addr
    except Exception as e:
        print(f"[EVENT] Exception in handle_initial_connection: {e}")
        try: conn.close()
        except: pass
        return None, None, None

def wait_for_reconnect(username, old_session, mode):
    """
    Waits up to RECONNECT_TIMEOUT seconds for the player to reconnect.
    Returns new (conn, addr) if reconnected, else None.
    """
    start_time = time.time()
    while time.time() - start_time < RECONNECT_TIMEOUT:
        with player_sessions_lock:
            session = player_sessions.get(username)
            if session and session.get('reconnected'):
                # Got a new connection
                conn = session['conn']
                addr = session['addr']
                session['reconnected'] = False  # Reset for future disconnects
                return conn, addr
        time.sleep(0.5)
    return None, None

def single_player(conn, addr, username):
    try:
        seq_send = 0
        seq_recv = 0
        print(f"[EVENT] Starting single player game for {addr}")

        def send(msg):
            nonlocal seq_send
            send_packet(conn, seq_send, PKT_TYPE_GAME, msg)
            seq_send += 1

        def recv():
            nonlocal seq_recv
            s, pkt_type, payload = recv_packet_handle_chat(conn, username)
            seq_recv = s
            return payload

        # Add instruction for ship placement
        instruction = (
            "INSTRUCTION: To place a ship, type: place <start_coord> <orientation> <ship_name>\n"
            "Example: place b6 v carrier\n"
        )
        send(instruction)

        class WFileWrapper:
            def write(self, msg):
                send(msg)
            def flush(self):
                pass

        class RFileWrapper:
            def readline(self):
                return recv()

        # Check if we have a saved game state for this player
        restored_game = False
        board = None
        with single_player_games_lock:
            if username in single_player_games:
                game_state = single_player_games[username]
                if game_state.get('game_started', False):
                    board = game_state.get('board')
                    ships_placed = game_state.get('ships_placed', False)
                    restored_game = True
                    print(f"[INFO] Restoring saved game for {username}")
                    # Send game restoration message to client
                    send("GAME_RESTORED: Your previous game state has been restored.")
                    if ships_placed:
                        send("SHIPS_PLACED: Your ships have been restored to their previous positions.")
                    else:
                        send("SHIPS_NOT_PLACED: You need to place your ships.")

        # Define a hook to save the game state during gameplay
        def save_state_hook(board, ships_placed, game_started):
            with single_player_games_lock:
                single_player_games[username] = {
                    'board': board,
                    'ships_placed': ships_placed,
                    'game_started': game_started,
                    'last_updated': time.time()
                }
            print(f"[INFO] Saved game state for {username}")

        # Run the game with the restored board if available
        run_single_player_game_online(RFileWrapper(), WFileWrapper(), board=board, save_state_hook=save_state_hook)
        print(f"[EVENT] Finished single player game for {addr}")
        
        # If game completed successfully, clean up the saved state
        with single_player_games_lock:
            if username in single_player_games:
                del single_player_games[username]
                print(f"[INFO] Removed completed game state for {username}")
                
    except Exception as e:
        print(f"[WARN] Single player client {addr} ({username}) disconnected: {e}")
        # Wait for reconnection
        with player_sessions_lock:
            player_sessions[username]['disconnected'] = True
        print(f"[INFO] Waiting {RECONNECT_TIMEOUT}s for {username} to reconnect...")
        new_conn, new_addr = wait_for_reconnect(username, player_sessions[username], mode="1")
        if new_conn:
            print(f"[INFO] {username} reconnected from {new_addr}. Resuming game.")

            single_player(new_conn, new_addr, username)
        else:
            print(f"[INFO] {username} did not reconnect in time. Forfeiting game.")
    finally:
        conn.close()
        print(f"[INFO] Single player client {addr} ({username}) connection closed.")

def two_player_game(conn1, addr1, conn2, addr2, username1, username2):
    global game_running
    winner_conn = None
    winner_addr = None
    last_winner_addr = None
    game_key = tuple(sorted([username1, username2]))
    try:
        # --- NEW: Mark both players as connected in game state ---
        game_state = games.get(game_key)
        if not game_state:
            board1 = Board(BOARD_SIZE)
            board2 = Board(BOARD_SIZE)
            turn = 0
            placed1 = False
            placed2 = False
            games[game_key] = {
                'board1': board1,
                'board2': board2,
                'turn': turn,
                'placed1': placed1,
                'placed2': placed2,
                'connected': {username1: True, username2: True},
                'conns': {username1: conn1, username2: conn2},
                'addrs': {username1: addr1, username2: addr2},
                'waiting_reconnect': False,
                'last_disconnect_time': None
            }
        else:
            board1 = game_state['board1']
            board2 = game_state['board2']
            turn = game_state['turn']
            placed1 = game_state.get('placed1', False)
            placed2 = game_state.get('placed2', False)
            game_state['connected'][username1] = True
            game_state['connected'][username2] = True
            game_state['conns'][username1] = conn1
            game_state['conns'][username2] = conn2
            game_state['addrs'][username1] = addr1
            game_state['addrs'][username2] = addr2
            game_state['waiting_reconnect'] = False
            game_state['last_disconnect_time'] = None

        seq_send1 = 0
        seq_recv1 = 0
        seq_send2 = 0
        seq_recv2 = 0

        def send1(msg):
            nonlocal seq_send1
            send_packet(conn1, seq_send1, PKT_TYPE_GAME, msg)
            seq_send1 += 1

        def send2(msg):
            nonlocal seq_send2
            send_packet(conn2, seq_send2, PKT_TYPE_GAME, msg)
            seq_send2 += 1

        def recv1():
            nonlocal seq_recv1
            s, pkt_type, payload = recv_packet_handle_chat(conn1, username1)
            seq_recv1 = s
            return payload

        def recv2():
            nonlocal seq_recv2
            s, pkt_type, payload = recv_packet_handle_chat(conn2, username2)
            seq_recv2 = s
            return payload

        class WFileWrapper1:
            def write(self, msg):
                send1(msg)
            def flush(self):
                pass

        class WFileWrapper2:
            def write(self, msg):
                send2(msg)
            def flush(self):
                pass

        class RFileWrapper1:
            def readline(self):
                return recv1()

        class RFileWrapper2:
            def readline(self):
                return recv2()

        print(f"[EVENT] Starting two player game for {addr1} and {addr2}")
        instruction = (
            "INSTRUCTION: To place a ship, type: place <start_coord> <orientation> <ship_name>\n"
            "Example: place b6 v carrier\n"
        )
        send1(instruction)
        send2(instruction)
        def lobby_broadcast(msg):
            with waiting_players_lock:
                for c, a, u in waiting_lines:
                    try:
                        lobby_wfile = c.makefile('w')
                        lobby_wfile.write(msg + "\n")
                        lobby_wfile.flush()
                    except Exception:
                        pass
        with player_sessions_lock:
            player_sessions[username1]['in_game'] = True
            player_sessions[username2]['in_game'] = True
        game_running.set()

        # --- Game state restoration logic ---
        game_state = games.get(game_key)
        if not game_state:
            # New game state
            board1 = Board(BOARD_SIZE)
            board2 = Board(BOARD_SIZE)
            turn = 0
            placed1 = False
            placed2 = False
            games[game_key] = {
                'board1': board1,
                'board2': board2,
                'turn': turn,
                'placed1': placed1,
                'placed2': placed2
            }
        else:
            board1 = game_state['board1']
            board2 = game_state['board2']
            turn = game_state['turn']
            placed1 = game_state.get('placed1', False)
            placed2 = game_state.get('placed2', False)

        # Pass state to battleship logic
        def save_state_hook(board1, board2, turn, placed1, placed2):
            games[game_key]['board1'] = board1
            games[game_key]['board2'] = board2
            games[game_key]['turn'] = turn
            games[game_key]['placed1'] = placed1
            games[game_key]['placed2'] = placed2

        # --- NEW: Wrap run_two_player_game_online to handle disconnects and reconnections ---
        def player_disconnected_callback(username):
            game_state = games.get(game_key)
            if game_state:
                game_state['connected'][username] = False
                # --- Set waiting_reconnect immediately on disconnect ---
                game_state['waiting_reconnect'] = True
                game_state['last_disconnect_time'] = time.time()

        def send_info_to_players(disconnected, connected, game_state):
            try:
                if disconnected and connected:
                    loser = disconnected[0]
                    winner = connected[0]
                    loser_conn = game_state['conns'][loser]
                    winner_conn = game_state['conns'][winner]
                    winner_conn.sendall(build_packet(0, PKT_TYPE_GAME, b"INFO: Opponent disconnected. Waiting up to 60 seconds for them to reconnect..."))
                    try:
                        loser_conn.sendall(build_packet(0, PKT_TYPE_GAME, b"INFO: You have been disconnected. If you reconnect within 60 seconds, you can resume the game."))
                    except Exception:
                        pass
            except Exception:
                pass

        # --- NEW: Use a dedicated function to run the game logic ---
        def run_game():
            run_two_player_game_online(
                RFileWrapper1(), WFileWrapper1(),
                RFileWrapper2(), WFileWrapper2(),
                lobby_broadcast=lobby_broadcast,
                usernames=(username1, username2),
                board1=board1,
                board2=board2,
                turn=turn,
                placed1=placed1,
                placed2=placed2,
                save_state_hook=save_state_hook,
                player_disconnected_callback=player_disconnected_callback
            )

        game_thread = threading.Thread(target=run_game)
        game_thread.start()

        # --- Monitor disconnects while the game is running ---
        game_state = games.get(game_key)
        disconnect_timeout_started = False
        disconnect_start_time = None
        disconnected_user = None
        connected_user = None

        while True:
            if not game_thread.is_alive():
                break
            if game_state:
                disconnected = [u for u, c in game_state['connected'].items() if not c]
                connected = [u for u, c in game_state['connected'].items() if c]
                # Start timeout as soon as one player disconnects
                if not disconnect_timeout_started and len(disconnected) == 1 and len(connected) == 1:
                    disconnect_timeout_started = True
                    disconnect_start_time = time.time()
                    disconnected_user = disconnected[0]
                    connected_user = connected[0]
                    print(f"[INFO] Waiting 60s for {disconnected_user} to reconnect...")
                    send_info_to_players([disconnected_user], [connected_user], game_state)
                # If timeout started, check for reconnect or timeout expiry
                if disconnect_timeout_started:
                    # If both disconnected, break immediately
                    if len(connected) == 0 and len(disconnected) == 2:
                        print(f"[INFO] Both players at {addr1} and {addr2} QUIT or disconnected during the game or ship placement.")
                        break
                    # If reconnected, resume game
                    if all(game_state['connected'].values()):
                        print(f"[INFO] {', '.join(game_state['connected'].keys())} reconnected for game {game_key}.")
                        print(f"[INFO] Both players reconnected for game {game_key}.")
                        disconnect_timeout_started = False
                        disconnect_start_time = None
                        disconnected_user = None
                        connected_user = None
                    # If timeout expired, forfeit
                    elif time.time() - disconnect_start_time >= RECONNECT_TIMEOUT:
                        print(f"[INFO] {disconnected_user} did not reconnect in time. {connected_user} wins by forfeit.")
                        try:
                            winner_conn = game_state['conns'][connected_user]
                            winner_addr = game_state['addrs'][connected_user]
                            winner_username = connected_user
                            winner_conn.sendall(build_packet(0, PKT_TYPE_GAME, b"OPPONENT_TIMEOUT. You win!"))
                        except Exception:
                            winner_conn = None
                            winner_addr = None
                            winner_username = None
                        # End the game and requeue the winner for next match
                        game_state['waiting_reconnect'] = False
                        del games[game_key]
                        if winner_conn and winner_conn.fileno() != -1:
                            with waiting_players_lock:
                                if (winner_conn, winner_addr, winner_username) not in waiting_lines:
                                    waiting_lines.insert(0, (winner_conn, winner_addr, winner_username))
                        # --- Immediately break so lobby_manager can match the winner ---
                        break
            time.sleep(0.5)

        # --- Cleanup and lobby requeue logic ---
    except Exception as e:
        print(f"[ERROR] Exception during game: {e}")
        # --- Remove immediate win/forfeit logic here ---
        print(f"[INFO] Notified remaining player(s) of win and returning to lobby.")
    finally:
        with player_sessions_lock:
            player_sessions[username1]['in_game'] = False
            player_sessions[username2]['in_game'] = False
        try:
            # Remove both players from waiting_lines to prevent infinite rematch loop
            with waiting_players_lock:
                waiting_lines[:] = [item for item in waiting_lines if item[0] not in (conn1, conn2)]
            # Only check for disconnects if the game did NOT end normally
            both_alive = (conn1.fileno() != -1 and conn2.fileno() != -1)
            # Extra check: try to send a ping to both players to confirm they are really alive
            if both_alive:
                try:
                    conn1.send(b"PING\n")
                    conn2.send(b"PING\n")
                except Exception:
                    both_alive = False
            if both_alive:
                print(f"[INFO] Both players at {addr1} and {addr2} are still connected, game ended normally.")
                with waiting_players_lock:
                    # FIX: Always append (conn, addr, username)
                    waiting_lines.insert(0, (conn1, addr1, username1))
                    waiting_lines.append((conn2, addr2, username2))
                print(f"[INFO] Two-player game between {addr1} and {addr2} ended. Players returned to lobby if still connected.")
            else:
                # Improved: check fileno and try to send/recv to determine who is really disconnected
                still_connected = []
                disconnected = []
                for conn, addr in [(conn1, addr1), (conn2, addr2)]:
                    alive = False
                    if conn.fileno() != -1:
                        try:
                            conn.setblocking(False)
                            try:
                                conn.send(b"PING\n")
                                alive = True
                            except Exception:
                                try:
                                    data = conn.recv(1, socket.MSG_PEEK)
                                    if data:
                                        alive = True
                                except Exception:
                                    alive = False
                        finally:
                            try: conn.setblocking(True)
                            except Exception: pass
                    if alive:
                        still_connected.append((conn, addr))
                    else:
                        disconnected.append((conn, addr))

                # FIX: When requeueing winner, include username
                if len(still_connected) == 1 and len(disconnected) == 1:
                    winner_conn, winner_addr = still_connected[0]
                    quitter_conn, quitter_addr = disconnected[0]
                    # Find winner's username
                    winner_username = None
                    if (winner_conn, winner_addr) == (conn1, addr1):
                        winner_username = username1
                    elif (winner_conn, winner_addr) == (conn2, addr2):
                        winner_username = username2
                    print(f"[INFO] Player at {winner_addr} WON (opponent timeout/disconnect, waiting for next match or Ctrl+C to exit).")
                    print(f"[INFO] Player at {quitter_addr} QUIT or disconnected during the game or ship placement.")
                    # --- FIX: Requeue the winner for next match ---
                    with waiting_players_lock:
                        if (winner_conn, winner_addr, winner_username) not in waiting_lines and winner_conn.fileno() != -1:
                            waiting_lines.insert(0, (winner_conn, winner_addr, winner_username))
                    try: quitter_conn.close()
                    except Exception: pass
                    print(f"[INFO] Two-player game between {addr1} and {addr2} ended due to disconnect/timeout.")
                elif len(still_connected) == 0 and len(disconnected) == 2:
                    print(f"[INFO] Both players at {addr1} and {addr2} QUIT or disconnected during the game or ship placement.")
                    for conn, addr in disconnected:
                        try: conn.close()
                        except Exception: pass
                    print(f"[INFO] Two-player game between {addr1} and {addr2} ended due to both disconnecting.")
                elif len(still_connected) == 2:
                    print(f"[INFO] Both players at {addr1} and {addr2} are still connected (unexpected).")
                else:
                    for conn, addr in disconnected:
                        print(f"[INFO] Player at {addr} QUIT or disconnected during the game or ship placement.")
                        try: conn.close()
                        except Exception: pass
                    for conn, addr in still_connected:
                        print(f"[INFO] Player at {addr} is still connected.")
                    print(f"[INFO] Two-player game between {addr1} and {addr2} ended due to disconnect.")

            game_running.clear()
        except Exception as e:
            print(f"[ERROR] Error in two-player game setup: {e}")

def broadcast_chat(sender_username, message):
    # Defensive: ensure message is str
    if isinstance(message, bytes):
        message = message.decode('utf-8', errors='ignore')
    
    print(f"[EVENT] Broadcasting chat message from {sender_username}: '{message}'")
    print(f"[EVENT] Active connections count: {len(active_connections)}")
    
    packet = build_packet(0, PKT_TYPE_CHAT, f"{sender_username}: {message}".encode('utf-8'))
    with active_connections_lock:
        # Defensive: remove closed connections
        to_remove = []
        for idx, conn in enumerate(active_connections):
            try:
                fd = conn.fileno() if hasattr(conn, 'fileno') else 'unknown'
                print(f"[EVENT] Sending chat to connection {idx} (fd={fd})")
                conn.sendall(packet)
            except Exception as e:
                print(f"[EVENT] Failed to send chat to connection {idx}: {e}")
                to_remove.append(conn)
        
        for conn in to_remove:
            print(f"[EVENT] Removing dead connection from active_connections")
            active_connections.remove(conn)

def recv_packet_handle_chat(conn, username):
    """Receive a packet, handle chat packets inline, and return only game packets."""
    while True:
        try:
            seq, pkt_type, payload = recv_packet(conn)
        except Exception as e:
            # Defensive: treat disconnect as fatal
            print(f"[EVENT] Exception in recv_packet_handle_chat for {username}: {e}")
            raise ConnectionError("Client disconnected")
        
        if pkt_type == PKT_TYPE_CHAT:
            # Defensive: decode payload if it's bytes (for robustness)
            if isinstance(payload, bytes):
                payload = payload.decode('utf-8', errors='ignore')
            
            if payload is not None and payload.strip() != "":
                print(f"[EVENT] Received chat message from {username}: '{payload}'")
                broadcast_chat(username, payload)
            else:
                print(f"[EVENT] Received empty chat message from {username}")
            
            continue  # Wait for next packet
        
        if pkt_type is None or payload is None:
            print(f"[EVENT] Received invalid packet (type={pkt_type}, payload={payload}) from {username}")
            raise ConnectionError("Client disconnected")
        
        return seq, pkt_type, payload

def handle_initial_connection(conn, addr):
    """
    Handles the initial handshake to get the username.
    Returns (username, conn, addr) or (None, None, None) on failure.
    """
    try:
        seq = 0
        seq_recv = 0
        # Receive USERNAME packet
        seq_recv, pkt_type, payload = recv_packet(conn)
        if pkt_type != PKT_TYPE_GAME or not payload.startswith("USERNAME "):
            send_packet(conn, seq, PKT_TYPE_GAME, "ERROR: Must provide USERNAME <name> as first message.")
            conn.close()
            return None, None, None
        username = payload.strip().split(" ", 1)[1]
        if not username:
            send_packet(conn, seq, PKT_TYPE_GAME, "ERROR: Username cannot be empty.")
            conn.close()
            return None, None, None
        print(f"[EVENT] Received username: {username} from {addr}")
        # --- Send a protocol welcome/lobby message immediately after handshake ---
        send_packet(conn, seq+1, PKT_TYPE_GAME, "WELCOME! Waiting for game to start...")
        return username, conn, addr
    except Exception as e:
        print(f"[EVENT] Exception in handle_initial_connection: {e}")
        try: conn.close()
        except: pass
        return None, None, None

def wait_for_reconnect(username, old_session, mode):
    """
    Waits up to RECONNECT_TIMEOUT seconds for the player to reconnect.
    Returns new (conn, addr) if reconnected, else None.
    """
    start_time = time.time()
    while time.time() - start_time < RECONNECT_TIMEOUT:
        with player_sessions_lock:
            session = player_sessions.get(username)
            if session and session.get('reconnected'):
                # Got a new connection
                conn = session['conn']
                addr = session['addr']
                session['reconnected'] = False  # Reset for future disconnects
                return conn, addr
        time.sleep(0.5)
    return None, None

def single_player(conn, addr, username):
    try:
        seq_send = 0
        seq_recv = 0
        print(f"[EVENT] Starting single player game for {addr}")

        def send(msg):
            nonlocal seq_send
            send_packet(conn, seq_send, PKT_TYPE_GAME, msg)
            seq_send += 1

        def recv():
            nonlocal seq_recv
            s, pkt_type, payload = recv_packet_handle_chat(conn, username)
            seq_recv = s
            return payload

        # Add instruction for ship placement
        instruction = (
            "INSTRUCTION: To place a ship, type: place <start_coord> <orientation> <ship_name>\n"
            "Example: place b6 v carrier\n"
        )
        send(instruction)

        class WFileWrapper:
            def write(self, msg):
                send(msg)
            def flush(self):
                pass

        class RFileWrapper:
            def readline(self):
                return recv()

        run_single_player_game_online(RFileWrapper(), WFileWrapper())
        print(f"[EVENT] Finished single player game for {addr}")
    except Exception as e:
        print(f"[WARN] Single player client {addr} ({username}) disconnected: {e}")
        # Wait for reconnection
        with player_sessions_lock:
            player_sessions[username]['disconnected'] = True
        print(f"[INFO] Waiting {RECONNECT_TIMEOUT}s for {username} to reconnect...")
        new_conn, new_addr = wait_for_reconnect(username, player_sessions[username], mode="1")
        if new_conn:
            print(f"[INFO] {username} reconnected from {new_addr}. Resuming game.")
            # TODO: Restore game state if needed (for single player, may need to persist board)
            # For now, just restart a new game
            single_player(new_conn, new_addr, username)
        else:
            print(f"[INFO] {username} did not reconnect in time. Forfeiting game.")
    finally:
        conn.close()
        print(f"[INFO] Single player client {addr} ({username}) connection closed.")

def two_player_game(conn1, addr1, conn2, addr2, username1, username2):
    global game_running
    winner_conn = None
    winner_addr = None
    last_winner_addr = None
    game_key = tuple(sorted([username1, username2]))
    try:
        # --- NEW: Mark both players as connected in game state ---
        game_state = games.get(game_key)
        if not game_state:
            board1 = Board(BOARD_SIZE)
            board2 = Board(BOARD_SIZE)
            turn = 0
            placed1 = False
            placed2 = False
            games[game_key] = {
                'board1': board1,
                'board2': board2,
                'turn': turn,
                'placed1': placed1,
                'placed2': placed2,
                'connected': {username1: True, username2: True},
                'conns': {username1: conn1, username2: conn2},
                'addrs': {username1: addr1, username2: addr2},
                'waiting_reconnect': False,
                'last_disconnect_time': None
            }
        else:
            board1 = game_state['board1']
            board2 = game_state['board2']
            turn = game_state['turn']
            placed1 = game_state.get('placed1', False)
            placed2 = game_state.get('placed2', False)
            game_state['connected'][username1] = True
            game_state['connected'][username2] = True
            game_state['conns'][username1] = conn1
            game_state['conns'][username2] = conn2
            game_state['addrs'][username1] = addr1
            game_state['addrs'][username2] = addr2
            game_state['waiting_reconnect'] = False
            game_state['last_disconnect_time'] = None

        seq_send1 = 0
        seq_recv1 = 0
        seq_send2 = 0
        seq_recv2 = 0

        def send1(msg):
            nonlocal seq_send1
            send_packet(conn1, seq_send1, PKT_TYPE_GAME, msg)
            seq_send1 += 1

        def send2(msg):
            nonlocal seq_send2
            send_packet(conn2, seq_send2, PKT_TYPE_GAME, msg)
            seq_send2 += 1

        def recv1():
            nonlocal seq_recv1
            s, pkt_type, payload = recv_packet_handle_chat(conn1, username1)
            seq_recv1 = s
            return payload

        def recv2():
            nonlocal seq_recv2
            s, pkt_type, payload = recv_packet_handle_chat(conn2, username2)
            seq_recv2 = s
            return payload

        class WFileWrapper1:
            def write(self, msg):
                send1(msg)
            def flush(self):
                pass

        class WFileWrapper2:
            def write(self, msg):
                send2(msg)
            def flush(self):
                pass

        class RFileWrapper1:
            def readline(self):
                return recv1()

        class RFileWrapper2:
            def readline(self):
                return recv2()

        print(f"[EVENT] Starting two player game for {addr1} and {addr2}")
        instruction = (
            "INSTRUCTION: To place a ship, type: place <start_coord> <orientation> <ship_name>\n"
            "Example: place b6 v carrier\n"
        )
        send1(instruction)
        send2(instruction)
        def lobby_broadcast(msg):
            with waiting_players_lock:
                for c, a, u in waiting_lines:
                    try:
                        lobby_wfile = c.makefile('w')
                        lobby_wfile.write(msg + "\n")
                        lobby_wfile.flush()
                    except Exception:
                        pass
        with player_sessions_lock:
            player_sessions[username1]['in_game'] = True
            player_sessions[username2]['in_game'] = True
        game_running.set()

        # --- Game state restoration logic ---
        game_state = games.get(game_key)
        if not game_state:
            # New game state
            board1 = Board(BOARD_SIZE)
            board2 = Board(BOARD_SIZE)
            turn = 0
            placed1 = False
            placed2 = False
            games[game_key] = {
                'board1': board1,
                'board2': board2,
                'turn': turn,
                'placed1': placed1,
                'placed2': placed2
            }
        else:
            board1 = game_state['board1']
            board2 = game_state['board2']
            turn = game_state['turn']
            placed1 = game_state.get('placed1', False)
            placed2 = game_state.get('placed2', False)

        # Pass state to battleship logic
        def save_state_hook(board1, board2, turn, placed1, placed2):
            games[game_key]['board1'] = board1
            games[game_key]['board2'] = board2
            games[game_key]['turn'] = turn
            games[game_key]['placed1'] = placed1
            games[game_key]['placed2'] = placed2

        # --- NEW: Wrap run_two_player_game_online to handle disconnects and reconnections ---
        def player_disconnected_callback(username):
            game_state = games.get(game_key)
            if game_state:
                game_state['connected'][username] = False
                # --- Set waiting_reconnect immediately on disconnect ---
                game_state['waiting_reconnect'] = True
                game_state['last_disconnect_time'] = time.time()

        def send_info_to_players(disconnected, connected, game_state):
            try:
                if disconnected and connected:
                    loser = disconnected[0]
                    winner = connected[0]
                    loser_conn = game_state['conns'][loser]
                    winner_conn = game_state['conns'][winner]
                    winner_conn.sendall(build_packet(0, PKT_TYPE_GAME, b"INFO: Opponent disconnected. Waiting up to 60 seconds for them to reconnect..."))
                    try:
                        loser_conn.sendall(build_packet(0, PKT_TYPE_GAME, b"INFO: You have been disconnected. If you reconnect within 60 seconds, you can resume the game."))
                    except Exception:
                        pass
            except Exception:
                pass

        # --- NEW: Use a dedicated function to run the game logic ---
        def run_game():
            run_two_player_game_online(
                RFileWrapper1(), WFileWrapper1(),
                RFileWrapper2(), WFileWrapper2(),
                lobby_broadcast=lobby_broadcast,
                usernames=(username1, username2),
                board1=board1,
                board2=board2,
                turn=turn,
                placed1=placed1,
                placed2=placed2,
                save_state_hook=save_state_hook,
                player_disconnected_callback=player_disconnected_callback
            )

        game_thread = threading.Thread(target=run_game)
        game_thread.start()

        # --- Monitor disconnects while the game is running ---
        game_state = games.get(game_key)
        disconnect_timeout_started = False
        disconnect_start_time = None
        disconnected_user = None
        connected_user = None

        while True:
            if not game_thread.is_alive():
                break
            if game_state:
                disconnected = [u for u, c in game_state['connected'].items() if not c]
                connected = [u for u, c in game_state['connected'].items() if c]
                # Start timeout as soon as one player disconnects
                if not disconnect_timeout_started and len(disconnected) == 1 and len(connected) == 1:
                    disconnect_timeout_started = True
                    disconnect_start_time = time.time()
                    disconnected_user = disconnected[0]
                    connected_user = connected[0]
                    print(f"[INFO] Waiting 60s for {disconnected_user} to reconnect...")
                    send_info_to_players([disconnected_user], [connected_user], game_state)
                # If timeout started, check for reconnect or timeout expiry
                if disconnect_timeout_started:
                    # If both disconnected, break immediately
                    if len(connected) == 0 and len(disconnected) == 2:
                        print(f"[INFO] Both players at {addr1} and {addr2} QUIT or disconnected during the game or ship placement.")
                        break
                    # If reconnected, resume game
                    if all(game_state['connected'].values()):
                        print(f"[INFO] {', '.join(game_state['connected'].keys())} reconnected for game {game_key}.")
                        print(f"[INFO] Both players reconnected for game {game_key}.")
                        disconnect_timeout_started = False
                        disconnect_start_time = None
                        disconnected_user = None
                        connected_user = None
                    # If timeout expired, forfeit
                    elif time.time() - disconnect_start_time >= RECONNECT_TIMEOUT:
                        print(f"[INFO] {disconnected_user} did not reconnect in time. {connected_user} wins by forfeit.")
                        try:
                            winner_conn = game_state['conns'][connected_user]
                            winner_addr = game_state['addrs'][connected_user]
                            winner_username = connected_user
                            winner_conn.sendall(build_packet(0, PKT_TYPE_GAME, b"OPPONENT_TIMEOUT. You win!"))
                        except Exception:
                            winner_conn = None
                            winner_addr = None
                            winner_username = None
                        # End the game and requeue the winner for next match
                        game_state['waiting_reconnect'] = False
                        del games[game_key]
                        if winner_conn and winner_conn.fileno() != -1:
                            with waiting_players_lock:
                                if (winner_conn, winner_addr, winner_username) not in waiting_lines:
                                    waiting_lines.insert(0, (winner_conn, winner_addr, winner_username))
                        # --- Immediately break so lobby_manager can match the winner ---
                        break
            time.sleep(0.5)

        # --- Cleanup and lobby requeue logic ---
    except Exception as e:
        print(f"[ERROR] Exception during game: {e}")
        # --- Remove immediate win/forfeit logic here ---
        print(f"[INFO] Notified remaining player(s) of win and returning to lobby.")
    finally:
        with player_sessions_lock:
            player_sessions[username1]['in_game'] = False
            player_sessions[username2]['in_game'] = False
        try:
            # Remove both players from waiting_lines to prevent infinite rematch loop
            with waiting_players_lock:
                waiting_lines[:] = [item for item in waiting_lines if item[0] not in (conn1, conn2)]
            # Only check for disconnects if the game did NOT end normally
            both_alive = (conn1.fileno() != -1 and conn2.fileno() != -1)
            # Extra check: try to send a ping to both players to confirm they are really alive
            if both_alive:
                try:
                    conn1.send(b"PING\n")
                    conn2.send(b"PING\n")
                except Exception:
                    both_alive = False
            if both_alive:
                print(f"[INFO] Both players at {addr1} and {addr2} are still connected, game ended normally.")
                with waiting_players_lock:
                    # FIX: Always append (conn, addr, username)
                    waiting_lines.insert(0, (conn1, addr1, username1))
                    waiting_lines.append((conn2, addr2, username2))
                print(f"[INFO] Two-player game between {addr1} and {addr2} ended. Players returned to lobby if still connected.")
            else:
                # Improved: check fileno and try to send/recv to determine who is really disconnected
                still_connected = []
                disconnected = []
                for conn, addr in [(conn1, addr1), (conn2, addr2)]:
                    alive = False
                    if conn.fileno() != -1:
                        try:
                            conn.setblocking(False)
                            try:
                                conn.send(b"PING\n")
                                alive = True
                            except Exception:
                                try:
                                    data = conn.recv(1, socket.MSG_PEEK)
                                    if data:
                                        alive = True
                                except Exception:
                                    alive = False
                        finally:
                            try: conn.setblocking(True)
                            except Exception: pass
                    if alive:
                        still_connected.append((conn, addr))
                    else:
                        disconnected.append((conn, addr))

                # FIX: When requeueing winner, include username
                if len(still_connected) == 1 and len(disconnected) == 1:
                    winner_conn, winner_addr = still_connected[0]
                    quitter_conn, quitter_addr = disconnected[0]
                    # Find winner's username
                    winner_username = None
                    if (winner_conn, winner_addr) == (conn1, addr1):
                        winner_username = username1
                    elif (winner_conn, winner_addr) == (conn2, addr2):
                        winner_username = username2
                    print(f"[INFO] Player at {winner_addr} WON (opponent timeout/disconnect, waiting for next match or Ctrl+C to exit).")
                    print(f"[INFO] Player at {quitter_addr} QUIT or disconnected during the game or ship placement.")
                    # --- FIX: Requeue the winner for next match ---
                    with waiting_players_lock:
                        if (winner_conn, winner_addr, winner_username) not in waiting_lines and winner_conn.fileno() != -1:
                            waiting_lines.insert(0, (winner_conn, winner_addr, winner_username))
                    try: quitter_conn.close()
                    except Exception: pass
                    print(f"[INFO] Two-player game between {addr1} and {addr2} ended due to disconnect/timeout.")
                elif len(still_connected) == 0 and len(disconnected) == 2:
                    print(f"[INFO] Both players at {addr1} and {addr2} QUIT or disconnected during the game or ship placement.")
                    for conn, addr in disconnected:
                        try: conn.close()
                        except Exception: pass
                    print(f"[INFO] Two-player game between {addr1} and {addr2} ended due to both disconnecting.")
                elif len(still_connected) == 2:
                    print(f"[INFO] Both players at {addr1} and {addr2} are still connected (unexpected).")
                else:
                    for conn, addr in disconnected:
                        print(f"[INFO] Player at {addr} QUIT or disconnected during the game or ship placement.")
                        try: conn.close()
                        except Exception: pass
                    for conn, addr in still_connected:
                        print(f"[INFO] Player at {addr} is still connected.")
                    print(f"[INFO] Two-player game between {addr1} and {addr2} ended due to disconnect.")

            game_running.clear()
        except Exception as e:
            print(f"[ERROR] Error in two-player game setup: {e}")

def broadcast_chat(sender_username, message):
    # Defensive: ensure message is str
    if isinstance(message, bytes):
        message = message.decode('utf-8', errors='ignore')
    
    print(f"[EVENT] Broadcasting chat message from {sender_username}: '{message}'")
    print(f"[EVENT] Active connections count: {len(active_connections)}")
    
    packet = build_packet(0, PKT_TYPE_CHAT, f"{sender_username}: {message}".encode('utf-8'))
    with active_connections_lock:
        # Defensive: remove closed connections
        to_remove = []
        for idx, conn in enumerate(active_connections):
            try:
                print(f"[EVENT] Sending chat to connection {idx} (fd={conn.fileno() if hasattr(conn, 'fileno') else 'unknown'})")
                conn.sendall(packet)
            except Exception as e:
                print(f"[EVENT] Failed to send chat to connection {idx}: {e}")
                to_remove.append(conn)
        
        for conn in to_remove:
            print(f"[EVENT] Removing dead connection from active_connections")
            active_connections.remove(conn)

def recv_packet_handle_chat(conn, username):
    """Receive a packet, handle chat packets inline, and return only game packets."""
    while True:
        try:
            seq, pkt_type, payload = recv_packet(conn)
        except Exception as e:
            # Defensive: treat disconnect as fatal
            print(f"[EVENT] Exception in recv_packet_handle_chat for {username}: {e}")
            raise ConnectionError("Client disconnected")
        
        if pkt_type == PKT_TYPE_CHAT:
            # Defensive: decode payload if it's bytes (for robustness)
            if isinstance(payload, bytes):
                payload = payload.decode('utf-8', errors='ignore')
            
            if payload is not None and payload.strip() != "":
                print(f"[EVENT] Received chat message from {username}: '{payload}'")
                broadcast_chat(username, payload)
            else:
                print(f"[EVENT] Received empty chat message from {username}")
            
            continue  # Wait for next packet
        
        if pkt_type is None or payload is None:
            print(f"[EVENT] Received invalid packet (type={pkt_type}, payload={payload}) from {username}")
            raise ConnectionError("Client disconnected")
        
        return seq, pkt_type, payload

def game_manager(conn, addr, mode):
    username, conn, addr = handle_initial_connection(conn, addr)
    print(f"[EVENT] game_manager got username: {username}")
    if not username:
        print(f"[EVENT] Username handshake failed for {addr}")
        return

    # Add connection to active_connections for chat as soon as a valid user connects
    with active_connections_lock:
        if conn not in active_connections and conn.fileno() != -1:
            active_connections.append(conn)
            print(f"[EVENT] Added {addr} ({username}) to active_connections for chat right after connection")

    with player_sessions_lock:
        player_sessions[username] = {
            'conn': conn,
            'addr': addr,
            'last_active': time.time(),
            'disconnected': False,
            'reconnected': False,
            'in_game': False,
        }
    # --- FIX: Allow reconnect if player is marked as disconnected in any waiting_reconnect game ---
    if mode == "2":
        for game_key, game_state in games.items():
            if (
                username in game_key
                and game_state.get('waiting_reconnect')
                and not game_state['connected'][username]
            ):
                game_state['connected'][username] = True
                game_state['conns'][username] = conn
                game_state['addrs'][username] = addr
                print(f"[INFO] {username} reconnected to existing game {game_key}.")
                two_player_game(
                    game_state['conns'][game_key[0]], game_state['addrs'][game_key[0]],
                    game_state['conns'][game_key[1]], game_state['addrs'][game_key[1]],
                    game_key[0], game_key[1]
                )
                return
    # --- NEW: For single player, just play the game ---
    if mode == "1":
        single_player(conn, addr, username)
    else:
        with waiting_players_lock:
            waiting_lines.append((conn, addr, username))
            if game_running.is_set():
                try:
                    # Use protocol for lobby messages
                    send_packet(conn, 0, PKT_TYPE_GAME, "A game is currently in progress. You are in the lobby and will join the next game when it starts.")
                    send_packet(conn, 0, PKT_TYPE_GAME, "You can chat with 'chat <message>'.")
                except Exception:
                    print(f"[WARN] Failed to notify player at {addr}")
            else:
                try:
                    send_packet(conn, 0, PKT_TYPE_GAME, "Waiting for another player to join...")
                    send_packet(conn, 0, PKT_TYPE_GAME, "You can chat with 'chat <message>'.")
                except Exception:
                    print(f"[WARN] Failed to notify player at {addr}")
        # Wait for the connection to close (i.e., after a game or disconnect)
        try:
            seq_recv = 0
            while True:
                if conn.fileno() == -1:
                    # Connection closed
                    with waiting_players_lock:
                        if any(c == conn for c, _, _ in waiting_lines):
                            print(f"[INFO] Player at {addr} ({username}) QUIT or disconnected while in the lobby.")
                            waiting_lines[:] = [(c, a, u) for c, a, u in waiting_lines if c != conn]
                    
                    # Also remove from active_connections
                    with active_connections_lock:
                        if conn in active_connections:
                            active_connections.remove(conn)
                            print(f"[EVENT] Removed {addr} ({username}) from active_connections after disconnect")
                    break
                
                with waiting_players_lock:
                    # Only process if player is still in waiting_lines
                    if not any(c == conn for c, _, _ in waiting_lines):
                        # Player was moved to a game
                        break
                
                # Poll for messages to handle chat while in lobby
                try:
                    conn.setblocking(False)
                    try:
                        readable, _, _ = select.select([conn], [], [], 0.5)
                        if readable:
                            try:
                                seq, pkt_type, payload = recv_packet(conn)
                                if pkt_type == PKT_TYPE_CHAT:
                                    print(f"[EVENT] Received chat message from {username} in lobby: '{payload}'")
                                    broadcast_chat(username, payload)
                            except Exception as e:
                                print(f"[EVENT] Exception receiving from lobby player {username}: {e}")
                                # Player disconnected
                                with waiting_players_lock:
                                    waiting_lines[:] = [(c, a, u) for c, a, u in waiting_lines if c != conn]
                                with active_connections_lock:
                                    if conn in active_connections:
                                        active_connections.remove(conn)
                                        print(f"[EVENT] Removed {addr} ({username}) from active_connections after error")
                                break
                    except Exception:
                        pass
                finally:
                    try:
                        conn.setblocking(True)
                    except Exception:
                        # Socket is likely closed
                        break
                
                time.sleep(0.1) # Short sleep to prevent CPU thrashing
                
        except Exception as e:
            print(f"[EVENT] Exception in game_manager lobby wait for {username}: {e}")
            # Clean up if player disconnects
            with waiting_players_lock:
                waiting_lines[:] = [(c, a, u) for c, a, u in waiting_lines if c != conn]
            with active_connections_lock:
                if conn in active_connections:
                    active_connections.remove(conn)

def lobby_manager():
    while True:
        with waiting_players_lock:
            if len(waiting_lines) >= 2 and not game_running.is_set():
                # Remove any closed/disconnected connections from waiting_lines
                waiting_lines[:] = [(c, a, u) for (c, a, u) in waiting_lines if c.fileno() != -1]
                winner_in_lobby = None
                if len(waiting_lines) > 0:
                    winner_in_lobby = waiting_lines[0]
                if winner_in_lobby:
                    if len(waiting_lines) > 1:
                        next_opponent = waiting_lines[1]
                        msg = (
                            f"[LOBBY] Next match: {winner_in_lobby[1]} (last game winner) "
                            f"vs {next_opponent[1]}. The match will begin in FIVE SECONDS."
                        )
                    else:
                        msg = (
                            f"[LOBBY] Next match: {winner_in_lobby[1]} (last game winner) "
                            f"awaiting an opponent. The match will begin when another player joins."
                        )
                else:
                    if len(waiting_lines) >= 2:
                        msg = (
                            f"[LOBBY] Next match: {waiting_lines[0][1]} vs {waiting_lines[1][1]}. "
                            "The match will begin in FIVE SECONDS."
                        )
                    elif len(waiting_lines) == 1:
                        msg = (
                            f"[LOBBY] Next match: {waiting_lines[0][1]} awaiting an opponent. "
                            "The match will begin when another player joins."
                        )
                    else:
                        msg = "[LOBBY] Waiting for players to join for the next match."
                
                print(f"[EVENT] Lobby status: {msg}")
                
                # Broadcast to all lobby clients, including their queue position
                for idx, (conn, addr, username) in enumerate(waiting_lines):
                    try:
                        # Use protocol for lobby messages
                        send_packet(conn, idx, PKT_TYPE_GAME, f"{msg}\n[LOBBY] You are position {idx+1} in the queue.")
                        # Add a chat reminder message for lobby players
                        # Remind players they can chat
                        if len(waiting_lines) > 1:  # Only if there are other players to chat with
                            send_packet(conn, idx, PKT_TYPE_GAME, "[LOBBY] Remember: You can chat with other players using 'chat <message>'")
                    except Exception as e:
                        print(f"[EVENT] Failed to send lobby message to {username} at {addr}: {e}")
                        pass
                
                # If we have enough players, start a game after a delay
                if len(waiting_lines) >= 2:
                    print("[EVENT] Starting game countdown...")
                    time.sleep(5.0)
                    # Check again after delay in case players disconnected
                    if len(waiting_lines) >= 2:
                        (conn1, addr1, username1) = waiting_lines.pop(0)
                        (conn2, addr2, username2) = waiting_lines.pop(0)
                        print(f"[INFO] Starting new two player game between {username1} and {username2}.")
                        threading.Thread(target=two_player_game, args=(conn1, addr1, conn2, addr2, username1, username2), daemon=True).start()
                        
                        # Note: No need to add to active_connections here since we already added them when they connected
        
        threading.Event().wait(0.5) # Sleep the threaded game if no players

def main():
    mode = input ("Select mode: (1) Single player, (2) Two player: ").strip()
    print(f"[INFO] Server listening on {HOST}:{PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, PORT))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.listen(10)
        s.settimeout(1.0)
        
        # Print instructions about chat feature
        print("[INFO] Chat feature enabled. Players can chat by typing 'chat <message>'.")
        print("[INFO] All connected players will receive chat messages, including those in the lobby.")
        
        lobby_thread = None
        if mode == "2":
            lobby_thread = threading.Thread(target=lobby_manager)
            lobby_thread.daemon = True  # Make lobby thread a daemon so it doesn't block exit
            lobby_thread.start()
        try:
            while True:
                try:
                    conn, addr = s.accept()
                    print(f"[INFO] Player connected from {addr}")
                    threading.Thread(target=game_manager, args=(conn, addr, mode), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"[ERROR] Accept failed: {e}")
        except KeyboardInterrupt:
            print("\n[INFO] Server shutting down (Ctrl+C pressed).")
            s.close()
            # No join on daemon thread; process will exit immediately
            return

# HINT: For multiple clients, you'd need to:
# 1. Accept connections in a loop
# 2. Handle each client in a separate thread
# 3. Import threading and create a handle_client function

if __name__ == "__main__":
    main()