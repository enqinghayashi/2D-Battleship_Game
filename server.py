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

def send_packet(conn, seq, pkt_type, msg):
    """Send a packet with the given sequence, type, and string payload."""
    payload = msg.encode('utf-8')
    packet = build_packet(seq, pkt_type, payload)
    conn.sendall(packet)

def recv_packet(conn, terminate_event=None):
    """
    Receive a packet and return (seq, pkt_type, payload as str).
    Uses select for interruptible reads if terminate_event is provided.
    """
    header_size = 7  # 4+1+2
    payload_len_val = None # Renamed to avoid conflict with outer scope 'length' if any
    checksum_size = 4
    
    buffer = b''

    def read_bytes_interruptible(num_bytes):
        nonlocal buffer
        # Calculate how many bytes are needed from the socket
        # to satisfy num_bytes, considering what's already in buffer.
        # This interpretation was slightly off; num_bytes is what we want to *return*.
        # So, we need to ensure buffer has at least num_bytes.
        
        while len(buffer) < num_bytes:
            if terminate_event and terminate_event.is_set():
                raise ConnectionAbortedError("recv_packet terminated during read")
            
            # How much more to read from socket to potentially satisfy current request
            needed_from_socket = num_bytes - len(buffer)

            ready_to_read, _, _ = select.select([conn], [], [], 0.1) # 0.1s timeout
            if not ready_to_read:
                continue 

            # Read only what's needed, or what's available up to a reasonable chunk size
            # Max chunk to read to avoid overly large recv calls if num_bytes is huge.
            # However, for this protocol, num_bytes (header, then payload, then checksum) are small.
            chunk = conn.recv(needed_from_socket) 
            if not chunk:
                raise ConnectionError("Client disconnected")
            buffer += chunk
        
        data_to_return = buffer[:num_bytes]
        buffer = buffer[num_bytes:] 
        return data_to_return

    try:
        header_content = read_bytes_interruptible(header_size)
        seq, pkt_type, payload_len_val = struct.unpack("!IBH", header_content)
        
        payload_content = read_bytes_interruptible(payload_len_val)
        checksum_content = read_bytes_interruptible(checksum_size)
    except ConnectionAbortedError:
        raise
    except ConnectionError: 
        raise
    except Exception as e: 
        print(f"[WARN] recv_packet low-level read error: {e}")
        return None, None, None

    full_packet_data = header_content + payload_content + checksum_content
    try:
        parsed_seq, parsed_pkt_type, parsed_payload = parse_packet(full_packet_data)
        return parsed_seq, parsed_pkt_type, parsed_payload.decode('utf-8')
    except ValueError as e: 
        print(f"[WARN] recv_packet: parse_packet failed: {e}")
        return None, None, None
    except Exception as e:
        print(f"[WARN] recv_packet: unexpected error during final parse: {e}")
        return None, None, None

def recv_packet_handle_chat(conn, username, terminate_event=None): # Added terminate_event
    """Receive a packet, handle chat packets inline, and return only game packets."""
    while True:
        if terminate_event and terminate_event.is_set(): # Check event before blocking
            raise ConnectionAbortedError(f"recv_packet_handle_chat terminated for {username}")
        try:
            seq, pkt_type, payload = recv_packet(conn, terminate_event=terminate_event) # Pass event
        except ConnectionAbortedError:
            raise # Propagate if recv_packet was terminated
        except ConnectionError as e: # Catch disconnects from recv_packet
            print(f"[EVENT] ConnectionError in recv_packet_handle_chat for {username}: {e}")
            raise # Re-raise to be handled by caller
        except Exception as e:
            print(f"[EVENT] Unexpected Exception in recv_packet_handle_chat's call to recv_packet for {username}: {e}")
            raise ConnectionError(f"Client {username} disconnected or critical read error")
        
        if pkt_type == PKT_TYPE_CHAT:
            # Defensive: decode payload if it's bytes (for robustness)
            if isinstance(payload, bytes):
                payload = payload.decode('utf-8', errors='ignore')
            
            if payload is not None and payload.strip() != "":
                print(f"[EVENT] Received chat message from {username}: '{payload}'")
                broadcast_chat(username, payload)
            # else: print(f"[EVENT] Received empty or None chat message from {username}")
            continue
        
        if pkt_type is None or payload is None: # Indicates parse_packet error
            print(f"[EVENT] Received invalid packet (parse error: type={pkt_type}, payload={payload}) from {username}")
            raise ConnectionError(f"Invalid packet from {username} (parse error)")
        
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
    
    # Event to signal the game_thread (running battleship.py logic) to terminate
    this_game_terminate_event = threading.Event()

    try:
        game_state = games.get(game_key)
        if not game_state:
            board1 = Board(BOARD_SIZE)
            board2 = Board(BOARD_SIZE)
            turn = 0
            placed1 = False
            placed2 = False
            games[game_key] = {
                'board1': board1, 'board2': board2, 'turn': turn,
                'placed1': placed1, 'placed2': placed2,
                'connected': {username1: True, username2: True},
                'conns': {username1: conn1, username2: conn2},
                'addrs': {username1: addr1, username2: addr2},
                'waiting_reconnect': False, 'last_disconnect_time': None,
                'terminate_event': this_game_terminate_event # Store this game's terminate event
            }
        else:
            # Game state exists, this is likely a reconnect scenario.
            # The old game_thread should have been signaled by game_manager.
            board1 = game_state['board1']
            board2 = game_state['board2']
            turn = game_state['turn']
            placed1 = game_state.get('placed1', False)
            placed2 = game_state.get('placed2', False)
            
            game_state['connected'][username1] = True
            game_state['connected'][username2] = True # Assume both are now connected
            game_state['conns'][username1] = conn1
            game_state['conns'][username2] = conn2
            game_state['addrs'][username1] = addr1
            game_state['addrs'][username2] = addr2
            game_state['waiting_reconnect'] = False
            game_state['last_disconnect_time'] = None
            game_state['terminate_event'] = this_game_terminate_event # This new instance controls termination

        seq_send1, seq_recv1, seq_send2, seq_recv2 = 0, 0, 0, 0

        def send1(msg): nonlocal seq_send1; send_packet(conn1, seq_send1, PKT_TYPE_GAME, msg); seq_send1 += 1
        def send2(msg): nonlocal seq_send2; send_packet(conn2, seq_send2, PKT_TYPE_GAME, msg); seq_send2 += 1

        # Modified recv1 and recv2 to accept and pass the terminate_event
        def recv1(event_to_check):
            nonlocal seq_recv1
            s, pkt_type, payload = recv_packet_handle_chat(conn1, username1, terminate_event=event_to_check)
            seq_recv1 = s
            return payload

        def recv2(event_to_check):
            nonlocal seq_recv2
            s, pkt_type, payload = recv_packet_handle_chat(conn2, username2, terminate_event=event_to_check)
            seq_recv2 = s
            return payload

        # RFileWrappers now take the terminate_event
        class WFileWrapper1:
            def write(self, msg): send1(msg)
            def flush(self): pass

        class WFileWrapper2:
            def write(self, msg): send2(msg)
            def flush(self): pass

        class RFileWrapper1:
            def __init__(self, terminate_event): self.terminate_event = terminate_event
            def readline(self): return recv1(self.terminate_event)

        class RFileWrapper2:
            def __init__(self, terminate_event): self.terminate_event = terminate_event
            def readline(self): return recv2(self.terminate_event)

        print(f"[EVENT] Starting two player game for {addr1} ({username1}) and {addr2} ({username2})")
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
        def save_state_hook(b1, b2, t, p1, p2):
            gs = games.get(game_key)
            if gs: # Basic check
                gs.update({'board1': b1, 'board2': b2, 'turn': t, 'placed1': p1, 'placed2': p2})


        def player_disconnected_callback(username_dc):
            gs = games.get(game_key)
            if gs:
                if gs['connected'].get(username_dc, False): # Check if actually connected before marking
                    print(f"[CALLBACK] Player {username_dc} disconnected from game {game_key}.")
                    gs['connected'][username_dc] = False
                    gs['waiting_reconnect'] = True
                    gs['last_disconnect_time'] = time.time()
                    # Inform other player (simplified)
                    other_user = username2 if username_dc == username1 else username1
                    other_conn = conn2 if username_dc == username1 else conn1
                    if gs['connected'].get(other_user):
                        try:
                            send_packet(other_conn, 0, PKT_TYPE_GAME, "INFO: Opponent disconnected. Waiting for reconnect...")
                        except Exception as e:
                            print(f"[WARN] Failed to inform {other_user} of disconnect: {e}")
                # else:
                #    print(f"[CALLBACK] Player {username_dc} already marked disconnected or not in game {game_key}.")


        def run_game():
            try:
                run_two_player_game_online(
                    RFileWrapper1(this_game_terminate_event), WFileWrapper1(),
                    RFileWrapper2(this_game_terminate_event), WFileWrapper2(),
                    lobby_broadcast=lobby_broadcast,
                    usernames=(username1, username2),
                    board1=board1, board2=board2, turn=turn,
                    placed1=placed1, placed2=placed2,
                    save_state_hook=save_state_hook,
                    player_disconnected_callback=player_disconnected_callback
                )
            except ConnectionAbortedError: # Expected when this_game_terminate_event is set
                print(f"[INFO] Game logic for {game_key} aborted by terminate signal.")
            except ConnectionError as e:
                print(f"[INFO] Game logic for {game_key} ended due to connection error: {e}")
                # player_disconnected_callback should have been called by battleship.py's ConnectionError handling
            except Exception as e:
                print(f"[ERROR] run_game for {game_key} crashed: {e}")
            finally:
                print(f"[INFO] run_game_worker (battleship.py logic) for {game_key} finished.")
        
        game_thread = threading.Thread(target=run_game, daemon=True)
        game_thread.start()

        # Main monitoring loop for this two_player_game instance
        disconnect_timeout_started = False
        disconnect_start_time = None
        disconnected_user_monitor = None # Renamed to avoid clash with callback's var

        while True:
            if not game_thread.is_alive():
                print(f"[INFO] Game thread for {game_key} finished. Exiting supervisor loop.")
                break # Game ended normally or crashed, cleanup in finally

            current_gs_monitor = games.get(game_key)
            if not current_gs_monitor or current_gs_monitor.get('terminate_event') != this_game_terminate_event:
                # This instance is no longer the active supervisor for game_key (e.g., superseded by a reconnect)
                print(f"[INFO] Supervisor for {game_key} (instance with event {this_game_terminate_event}) superseded. Signaling its own game_thread to terminate.")
                this_game_terminate_event.set() # Signal its own game_thread
                game_thread.join(timeout=2.0)
                break # Exit this supervisor's loop

            if current_gs_monitor.get('waiting_reconnect'):
                if not disconnect_timeout_started:
                    # Find who is disconnected
                    temp_disconnected_user = None
                    for u, c_stat in current_gs_monitor['connected'].items():
                        if not c_stat: temp_disconnected_user = u; break
                    
                    if temp_disconnected_user:
                        disconnected_user_monitor = temp_disconnected_user
                        disconnect_timeout_started = True
                        disconnect_start_time = current_gs_monitor.get('last_disconnect_time', time.time())
                        print(f"[INFO] Supervisor for {game_key}: Detected {disconnected_user_monitor} disconnected. Timeout started.")
                    else: # Should not happen if waiting_reconnect is true
                        current_gs_monitor['waiting_reconnect'] = False # Reset

                if disconnect_timeout_started:
                    if all(current_gs_monitor['connected'].values()): # Player reconnected
                        print(f"[INFO] Supervisor for {game_key}: Player {disconnected_user_monitor} reconnected. Game_manager will handle new supervisor.")
                        # This supervisor instance will be superseded. The check for 'terminate_event' inequality will catch it.
                        disconnect_timeout_started = False
                        # No need to break here; the event inequality check will handle it.
                    elif time.time() - disconnect_start_time >= RECONNECT_TIMEOUT:
                        connected_user_monitor = None
                        for u, c_stat in current_gs_monitor['connected'].items():
                            if c_stat: connected_user_monitor = u; break
                        
                        print(f"[INFO] Supervisor for {game_key}: {disconnected_user_monitor} did not reconnect. {connected_user_monitor or 'Opponent'} wins by forfeit.")
                        if connected_user_monitor:
                            try:
                                other_conn = current_gs_monitor['conns'][connected_user_monitor]
                                send_packet(other_conn, 0, PKT_TYPE_GAME, "OPPONENT_TIMEOUT. You win!")
                            except Exception: pass
                        
                        this_game_terminate_event.set() # Signal game_thread to stop
                        game_thread.join(timeout=2.0)
                        # Mark for deletion in finally block by this active supervisor
                        current_gs_monitor['game_over_by_forfeit'] = True 
                        break # Exit supervisor loop to finally block
            else: # Not waiting_reconnect
                disconnect_timeout_started = False
            
            time.sleep(0.5)
        # End of supervisor's main while loop

    except Exception as e:
        print(f"[ERROR] Outer exception in two_player_game for {game_key}: {e}")
        if games.get(game_key, {}).get('terminate_event') == this_game_terminate_event:
            this_game_terminate_event.set() # Try to stop its game_thread
    finally:
        print(f"[INFO] two_player_game instance for {game_key} (event {this_game_terminate_event}) entering finally.")
        
        final_gs_check = games.get(game_key)
        is_still_active_supervisor = final_gs_check and final_gs_check.get('terminate_event') == this_game_terminate_event

        game_ended_by_this_supervisor = False
        if is_still_active_supervisor:
            if not game_thread.is_alive() or final_gs_check.get('game_over_by_forfeit'):
                game_ended_by_this_supervisor = True
        
        if game_ended_by_this_supervisor:
            print(f"[INFO] Active supervisor for {game_key} (event {this_game_terminate_event}) cleaning up.")
            # Re-queue logic (simplified, ensure players are valid)
            players_to_requeue = []
            was_forfeit = final_gs_check.get('game_over_by_forfeit', False)

            if was_forfeit:
                for p_user in [username1, username2]:
                    if final_gs_check['connected'].get(p_user): # Winner by forfeit
                        p_conn = final_gs_check['conns'].get(p_user)
                        p_addr = final_gs_check['addrs'].get(p_user)
                        if p_conn and p_conn.fileno() != -1: players_to_requeue.append((p_conn, p_addr, p_user))
            else: # Normal end
                for p_user in [username1, username2]:
                    # Check current connections from this supervisor's perspective
                    p_conn = conn1 if p_user == username1 else conn2
                    p_addr = addr1 if p_user == username1 else addr2
                    # A player might have disconnected just as game ended, so check fileno
                    if p_conn and p_conn.fileno() != -1:
                         players_to_requeue.append((p_conn, p_addr, p_user))
            
            with waiting_players_lock:
                waiting_lines[:] = [item for item in waiting_lines if item[2] not in (username1, username2)]
                for p_conn, p_addr, p_user in players_to_requeue:
                     if not any(wl_item[2] == p_user for wl_item in waiting_lines):
                        waiting_lines.append((p_conn, p_addr, p_user))
                        print(f"[INFO] Re-queued {p_user} by supervisor for {game_key}.")
            
            if game_key in games and games[game_key].get('terminate_event') == this_game_terminate_event:
                del games[game_key]
                print(f"[INFO] Game state for {game_key} deleted by its active supervisor.")
            
            with player_sessions_lock:
                if player_sessions.get(username1): player_sessions[username1]['in_game'] = False
                if player_sessions.get(username2): player_sessions[username2]['in_game'] = False
            game_running.clear()
        else:
            print(f"[INFO] Superseded/Inactive supervisor for {game_key} (event {this_game_terminate_event}) minimal cleanup.")
            if not this_game_terminate_event.is_set(): this_game_terminate_event.set()
            if game_thread.is_alive(): game_thread.join(timeout=1.0)
        
        print(f"[INFO] two_player_game instance for {game_key} (event {this_game_terminate_event}) finished execution.")

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

def recv_packet_handle_chat(conn, username, terminate_event=None): # Added terminate_event
    """Receive a packet, handle chat packets inline, and return only game packets."""
    while True:
        if terminate_event and terminate_event.is_set(): # Check event before blocking
            raise ConnectionAbortedError(f"recv_packet_handle_chat terminated for {username}")
        try:
            seq, pkt_type, payload = recv_packet(conn, terminate_event=terminate_event) # Pass event
        except ConnectionAbortedError:
            raise # Propagate if recv_packet was terminated
        except ConnectionError as e: # Catch disconnects from recv_packet
            print(f"[EVENT] ConnectionError in recv_packet_handle_chat for {username}: {e}")
            raise # Re-raise to be handled by caller
        except Exception as e:
            print(f"[EVENT] Unexpected Exception in recv_packet_handle_chat's call to recv_packet for {username}: {e}")
            raise ConnectionError(f"Client {username} disconnected or critical read error")
        
        if pkt_type == PKT_TYPE_CHAT:
            # Defensive: decode payload if it's bytes (for robustness)
            if isinstance(payload, bytes):
                payload = payload.decode('utf-8', errors='ignore')
            
            if payload is not None and payload.strip() != "":
                print(f"[EVENT] Received chat message from {username}: '{payload}'")
                broadcast_chat(username, payload)
            # else: print(f"[EVENT] Received empty or None chat message from {username}")
            continue
        
        if pkt_type is None or payload is None: # Indicates parse_packet error
            print(f"[EVENT] Received invalid packet (parse error: type={pkt_type}, payload={payload}) from {username}")
            raise ConnectionError(f"Invalid packet from {username} (parse error)")
        
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
    
    # Event to signal the game_thread (running battleship.py logic) to terminate
    this_game_terminate_event = threading.Event()

    try:
        game_state = games.get(game_key)
        if not game_state:
            board1 = Board(BOARD_SIZE)
            board2 = Board(BOARD_SIZE)
            turn = 0
            placed1 = False
            placed2 = False
            games[game_key] = {
                'board1': board1, 'board2': board2, 'turn': turn,
                'placed1': placed1, 'placed2': placed2,
                'connected': {username1: True, username2: True},
                'conns': {username1: conn1, username2: conn2},
                'addrs': {username1: addr1, username2: addr2},
                'waiting_reconnect': False, 'last_disconnect_time': None,
                'terminate_event': this_game_terminate_event # Store this game's terminate event
            }
        else:
            # Game state exists, this is likely a reconnect scenario.
            # The old game_thread should have been signaled by game_manager.
            board1 = game_state['board1']
            board2 = game_state['board2']
            turn = game_state['turn']
            placed1 = game_state.get('placed1', False)
            placed2 = game_state.get('placed2', False)
            
            game_state['connected'][username1] = True
            game_state['connected'][username2] = True # Assume both are now connected
            game_state['conns'][username1] = conn1
            game_state['conns'][username2] = conn2
            game_state['addrs'][username1] = addr1
            game_state['addrs'][username2] = addr2
            game_state['waiting_reconnect'] = False
            game_state['last_disconnect_time'] = None
            game_state['terminate_event'] = this_game_terminate_event # This new instance controls termination

        seq_send1, seq_recv1, seq_send2, seq_recv2 = 0, 0, 0, 0

        def send1(msg): nonlocal seq_send1; send_packet(conn1, seq_send1, PKT_TYPE_GAME, msg); seq_send1 += 1
        def send2(msg): nonlocal seq_send2; send_packet(conn2, seq_send2, PKT_TYPE_GAME, msg); seq_send2 += 1

        # Modified recv1 and recv2 to accept and pass the terminate_event
        def recv1(event_to_check):
            nonlocal seq_recv1
            s, pkt_type, payload = recv_packet_handle_chat(conn1, username1, terminate_event=event_to_check)
            seq_recv1 = s
            return payload

        def recv2(event_to_check):
            nonlocal seq_recv2
            s, pkt_type, payload = recv_packet_handle_chat(conn2, username2, terminate_event=event_to_check)
            seq_recv2 = s
            return payload

        # RFileWrappers now take the terminate_event
        class WFileWrapper1:
            def write(self, msg): send1(msg)
            def flush(self): pass

        class WFileWrapper2:
            def write(self, msg): send2(msg)
            def flush(self): pass

        class RFileWrapper1:
            def __init__(self, terminate_event): self.terminate_event = terminate_event
            def readline(self): return recv1(self.terminate_event)

        class RFileWrapper2:
            def __init__(self, terminate_event): self.terminate_event = terminate_event
            def readline(self): return recv2(self.terminate_event)

        print(f"[EVENT] Starting two player game for {addr1} ({username1}) and {addr2} ({username2})")
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
        def save_state_hook(b1, b2, t, p1, p2):
            gs = games.get(game_key)
            if gs: # Basic check
                gs.update({'board1': b1, 'board2': b2, 'turn': t, 'placed1': p1, 'placed2': p2})


        def player_disconnected_callback(username_dc):
            gs = games.get(game_key)
            if gs:
                if gs['connected'].get(username_dc, False): # Check if actually connected before marking
                    print(f"[CALLBACK] Player {username_dc} disconnected from game {game_key}.")
                    gs['connected'][username_dc] = False
                    gs['waiting_reconnect'] = True
                    gs['last_disconnect_time'] = time.time()
                    # Inform other player (simplified)
                    other_user = username2 if username_dc == username1 else username1
                    other_conn = conn2 if username_dc == username1 else conn1
                    if gs['connected'].get(other_user):
                        try:
                            send_packet(other_conn, 0, PKT_TYPE_GAME, "INFO: Opponent disconnected. Waiting for reconnect...")
                        except Exception as e:
                            print(f"[WARN] Failed to inform {other_user} of disconnect: {e}")
                # else:
                #    print(f"[CALLBACK] Player {username_dc} already marked disconnected or not in game {game_key}.")


        def run_game():
            try:
                run_two_player_game_online(
                    RFileWrapper1(this_game_terminate_event), WFileWrapper1(),
                    RFileWrapper2(this_game_terminate_event), WFileWrapper2(),
                    lobby_broadcast=lobby_broadcast,
                    usernames=(username1, username2),
                    board1=board1, board2=board2, turn=turn,
                    placed1=placed1, placed2=placed2,
                    save_state_hook=save_state_hook,
                    player_disconnected_callback=player_disconnected_callback
                )
            except ConnectionAbortedError: # Expected when this_game_terminate_event is set
                print(f"[INFO] Game logic for {game_key} aborted by terminate signal.")
            except ConnectionError as e:
                print(f"[INFO] Game logic for {game_key} ended due to connection error: {e}")
                # player_disconnected_callback should have been called by battleship.py's ConnectionError handling
            except Exception as e:
                print(f"[ERROR] run_game for {game_key} crashed: {e}")
            finally:
                print(f"[INFO] run_game_worker (battleship.py logic) for {game_key} finished.")
        
        game_thread = threading.Thread(target=run_game, daemon=True)
        game_thread.start()

        # Main monitoring loop for this two_player_game instance
        disconnect_timeout_started = False
        disconnect_start_time = None
        disconnected_user_monitor = None # Renamed to avoid clash with callback's var

        while True:
            if not game_thread.is_alive():
                print(f"[INFO] Game thread for {game_key} finished. Exiting supervisor loop.")
                break # Game ended normally or crashed, cleanup in finally

            current_gs_monitor = games.get(game_key)
            if not current_gs_monitor or current_gs_monitor.get('terminate_event') != this_game_terminate_event:
                # This instance is no longer the active supervisor for game_key (e.g., superseded by a reconnect)
                print(f"[INFO] Supervisor for {game_key} (instance with event {this_game_terminate_event}) superseded. Signaling its own game_thread to terminate.")
                this_game_terminate_event.set() # Signal its own game_thread
                game_thread.join(timeout=2.0)
                break # Exit this supervisor's loop

            if current_gs_monitor.get('waiting_reconnect'):
                if not disconnect_timeout_started:
                    # Find who is disconnected
                    temp_disconnected_user = None
                    for u, c_stat in current_gs_monitor['connected'].items():
                        if not c_stat: temp_disconnected_user = u; break
                    
                    if temp_disconnected_user:
                        disconnected_user_monitor = temp_disconnected_user
                        disconnect_timeout_started = True
                        disconnect_start_time = current_gs_monitor.get('last_disconnect_time', time.time())
                        print(f"[INFO] Supervisor for {game_key}: Detected {disconnected_user_monitor} disconnected. Timeout started.")
                    else: # Should not happen if waiting_reconnect is true
                        current_gs_monitor['waiting_reconnect'] = False # Reset

                if disconnect_timeout_started:
                    if all(current_gs_monitor['connected'].values()): # Player reconnected
                        print(f"[INFO] Supervisor for {game_key}: Player {disconnected_user_monitor} reconnected. Game_manager will handle new supervisor.")
                        # This supervisor instance will be superseded. The check for 'terminate_event' inequality will catch it.
                        disconnect_timeout_started = False
                        # No need to break here; the event inequality check will handle it.
                    elif time.time() - disconnect_start_time >= RECONNECT_TIMEOUT:
                        connected_user_monitor = None
                        for u, c_stat in current_gs_monitor['connected'].items():
                            if c_stat: connected_user_monitor = u; break
                        
                        print(f"[INFO] Supervisor for {game_key}: {disconnected_user_monitor} did not reconnect. {connected_user_monitor or 'Opponent'} wins by forfeit.")
                        if connected_user_monitor:
                            try:
                                other_conn = current_gs_monitor['conns'][connected_user_monitor]
                                send_packet(other_conn, 0, PKT_TYPE_GAME, "OPPONENT_TIMEOUT. You win!")
                            except Exception: pass
                        
                        this_game_terminate_event.set() # Signal game_thread to stop
                        game_thread.join(timeout=2.0)
                        # Mark for deletion in finally block by this active supervisor
                        current_gs_monitor['game_over_by_forfeit'] = True 
                        break # Exit supervisor loop to finally block
            else: # Not waiting_reconnect
                disconnect_timeout_started = False
            
            time.sleep(0.5)
        # End of supervisor's main while loop

    except Exception as e:
        print(f"[ERROR] Outer exception in two_player_game for {game_key}: {e}")
        if games.get(game_key, {}).get('terminate_event') == this_game_terminate_event:
            this_game_terminate_event.set() # Try to stop its game_thread
    finally:
        print(f"[INFO] two_player_game instance for {game_key} (event {this_game_terminate_event}) entering finally.")
        
        final_gs_check = games.get(game_key)
        is_still_active_supervisor = final_gs_check and final_gs_check.get('terminate_event') == this_game_terminate_event

        game_ended_by_this_supervisor = False
        if is_still_active_supervisor:
            if not game_thread.is_alive() or final_gs_check.get('game_over_by_forfeit'):
                game_ended_by_this_supervisor = True
        
        if game_ended_by_this_supervisor:
            print(f"[INFO] Active supervisor for {game_key} (event {this_game_terminate_event}) cleaning up.")
            # Re-queue logic (simplified, ensure players are valid)
            players_to_requeue = []
            was_forfeit = final_gs_check.get('game_over_by_forfeit', False)

            if was_forfeit:
                for p_user in [username1, username2]:
                    if final_gs_check['connected'].get(p_user): # Winner by forfeit
                        p_conn = final_gs_check['conns'].get(p_user)
                        p_addr = final_gs_check['addrs'].get(p_user)
                        if p_conn and p_conn.fileno() != -1: players_to_requeue.append((p_conn, p_addr, p_user))
            else: # Normal end
                for p_user in [username1, username2]:
                    # Check current connections from this supervisor's perspective
                    p_conn = conn1 if p_user == username1 else conn2
                    p_addr = addr1 if p_user == username1 else addr2
                    # A player might have disconnected just as game ended, so check fileno
                    if p_conn and p_conn.fileno() != -1:
                         players_to_requeue.append((p_conn, p_addr, p_user))
            
            with waiting_players_lock:
                waiting_lines[:] = [item for item in waiting_lines if item[2] not in (username1, username2)]
                for p_conn, p_addr, p_user in players_to_requeue:
                     if not any(wl_item[2] == p_user for wl_item in waiting_lines):
                        waiting_lines.append((p_conn, p_addr, p_user))
                        print(f"[INFO] Re-queued {p_user} by supervisor for {game_key}.")
            
            if game_key in games and games[game_key].get('terminate_event') == this_game_terminate_event:
                del games[game_key]
                print(f"[INFO] Game state for {game_key} deleted by its active supervisor.")
            
            with player_sessions_lock:
                if player_sessions.get(username1): player_sessions[username1]['in_game'] = False
                if player_sessions.get(username2): player_sessions[username2]['in_game'] = False
            game_running.clear()
        else:
            print(f"[INFO] Superseded/Inactive supervisor for {game_key} (event {this_game_terminate_event}) minimal cleanup.")
            if not this_game_terminate_event.is_set(): this_game_terminate_event.set()
            if game_thread.is_alive(): game_thread.join(timeout=1.0)
        
        print(f"[INFO] two_player_game instance for {game_key} (event {this_game_terminate_event}) finished execution.")

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

def recv_packet_handle_chat(conn, username, terminate_event=None): # Added terminate_event
    """Receive a packet, handle chat packets inline, and return only game packets."""
    while True:
        if terminate_event and terminate_event.is_set(): # Check event before blocking
            raise ConnectionAbortedError(f"recv_packet_handle_chat terminated for {username}")
        try:
            seq, pkt_type, payload = recv_packet(conn, terminate_event=terminate_event) # Pass event
        except ConnectionAbortedError:
            raise # Propagate if recv_packet was terminated
        except ConnectionError as e: # Catch disconnects from recv_packet
            print(f"[EVENT] ConnectionError in recv_packet_handle_chat for {username}: {e}")
            raise # Re-raise to be handled by caller
        except Exception as e:
            print(f"[EVENT] Unexpected Exception in recv_packet_handle_chat's call to recv_packet for {username}: {e}")
            raise ConnectionError(f"Client {username} disconnected or critical read error")
        
        if pkt_type == PKT_TYPE_CHAT:
            # Defensive: decode payload if it's bytes (for robustness)
            if isinstance(payload, bytes):
                payload = payload.decode('utf-8', errors='ignore')
            
            if payload is not None and payload.strip() != "":
                print(f"[EVENT] Received chat message from {username}: '{payload}'")
                broadcast_chat(username, payload)
            # else: print(f"[EVENT] Received empty or None chat message from {username}")
            continue
        
        if pkt_type is None or payload is None: # Indicates parse_packet error
            print(f"[EVENT] Received invalid packet (parse error: type={pkt_type}, payload={payload}) from {username}")
            raise ConnectionError(f"Invalid packet from {username} (parse error)")
        
        return seq, pkt_type, payload

def game_manager(conn, addr, mode):
    username, conn_after_handshake, addr_after_handshake = handle_initial_connection(conn, addr)
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