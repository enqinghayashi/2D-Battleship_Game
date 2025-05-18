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
from battleship import run_single_player_game_online, run_two_player_game_online, Board, BOARD_SIZE, SHIPS
from protocol.encryption import encrypt_message, decrypt_message


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

def safe_recv(rfile):
    line = rfile.readline()
    if not line:
        raise ConnectionError("Disconnected.")
    raw = line.strip()
    print(f"[DEBUG] safe_recv raw: {raw}") 

    try:
        decrypted = decrypt_message(raw)
        print(f"[DEBUG] safe_recv decrypted: {decrypted}")
        return decrypted
    except Exception as e:
        print(f"[DECRYPT-ERROR] in safe_recv | raw={raw} | error={e}")
        raise

# Encrypts messages written to the socket
class EncryptedWFileWrapper:
    def __init__(self, wfile):
        self.wfile = wfile

    def write(self, msg):
        encrypted = encrypt_message(msg)
        print(f"[DEBUG] sending encrypted: {msg}") 
        self.wfile.write(encrypted + '\n')

    def flush(self):
        self.wfile.flush()


def handle_initial_connection(conn, addr):
    """
    Handles the initial handshake to get the username.
    Returns (username, conn, addr) or (None, None, None) on failure.
    """
    try:
        rfile = conn.makefile('r')
        wfile = EncryptedWFileWrapper(conn.makefile('w'))
        encrypted = rfile.readline()
        line = decrypt_message(encrypted.strip())
        if not line:
            conn.close()
            return None, None, None
        if not line.startswith("USERNAME "):
            wfile.write("ERROR: Must provide USERNAME <name> as first message.\n")
            wfile.flush()
            conn.close()
            return None, None, None
        username = line.strip().split(" ", 1)[1]
        if not username:
            wfile.write("ERROR: Username cannot be empty.\n")
            wfile.flush()
            conn.close()
            return None, None, None
        return username, conn, addr
    except Exception:
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
        rfile = conn.makefile('r')
        wfile = EncryptedWFileWrapper(conn.makefile('w'))
        print(f"[DEBUG] Starting single player game for {addr}")
        # Add instruction for ship placement
        instruction = (
            "INSTRUCTION: To place a ship, type: place <start_coord> <orientation> <ship_name>\n"
            "Example: place b6 v carrier\n"
        )
        wfile.write(instruction)
        wfile.flush()
        # Wrap the wfile to inject instruction on error
        class WFileWrapper:
            def __init__(self, wfile):
                self.wfile = wfile
            def write(self, msg):
                if msg.startswith("ERROR Invalid coordinate:"):
                    self.wfile.write(msg)
                    self.wfile.write(
                        "INSTRUCTION: To place a ship, type: place <start_coord> <orientation> <ship_name>\n"
                        "Example: place b6 v carrier\n"
                    )
                else:
                    self.wfile.write(msg)
            def flush(self):
                self.wfile.flush()
        run_single_player_game_online(rfile, WFileWrapper(wfile))
        print(f"[DEBUG] Finished single player game for {addr}")
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

        rfile1 = conn1.makefile('r')
        wfile1 = EncryptedWFileWrapper(conn1.makefile('w'))
        rfile2 = conn2.makefile('r')
        wfile2 = EncryptedWFileWrapper(conn2.makefile('w'))
        print(f"[DEBUG] Starting two player game for {addr1} and {addr2}")
        instruction = (
            "INSTRUCTION: To place a ship, type: place <start_coord> <orientation> <ship_name>\n"
            "Example: place b6 v carrier\n"
        )
        wfile1.write(instruction)
        wfile1.flush()
        wfile2.write(instruction)
        wfile2.flush()
        class WFileWrapper:
            def __init__(self, wfile):
                self.wfile = wfile
            def write(self, msg):
                if msg.startswith("ERROR Invalid coordinate:"):
                    self.wfile.write(msg)
                    self.wfile.write(
                        "INSTRUCTION: To place a ship, type: place <start_coord> <orientation> <ship_name>\n"
                        "Example: place b6 v carrier\n"
                    )
                else:
                    self.wfile.write(msg)
            def flush(self):
                self.wfile.flush()
        def lobby_broadcast(msg):
            with waiting_players_lock:
                for c, a, u in waiting_lines:
                    try:
                        lobby_wfile = EncryptedWFileWrapper(c.makefile('w'))
                        lobby_wfile.write(msg)
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

        def run_game():
            run_two_player_game_online(
                rfile1, WFileWrapper(wfile1),
                rfile2, WFileWrapper(wfile2),
                lobby_broadcast=lobby_broadcast,
                usernames=(username1, username2),
                board1=board1,
                board2=board2,
                turn=turn,
                placed1=placed1,
                placed2=placed2,
                save_state_hook=save_state_hook,
                player_disconnected_callback=player_disconnected_callback,
                recv1=safe_recv,
                recv2=safe_recv
            )

        game_thread = threading.Thread(target=run_game)
        game_thread.start()
        game_thread.join()

        # --- Wait for possible reconnection if a disconnect occurred ---
        game_state = games.get(game_key)
        if game_state:
            if not all(game_state['connected'].values()):
                # At least one player disconnected, start timeout for possible reconnection
                game_state['waiting_reconnect'] = True
                game_state['last_disconnect_time'] = time.time()
                for _ in range(RECONNECT_TIMEOUT * 2):  # check every 0.5s
                    time.sleep(0.5)
                    if all(game_state['connected'].values()):
                        # Both reconnected, resume game
                        print(f"[INFO] Both players reconnected for game {game_key}.")
                        return two_player_game(
                            game_state['conns'][game_key[0]], game_state['addrs'][game_key[0]],
                            game_state['conns'][game_key[1]], game_state['addrs'][game_key[1]],
                            game_key[0], game_key[1]
                        )
                # If still not all connected, declare forfeit
                disconnected = [u for u, c in game_state['connected'].items() if not c]
                connected = [u for u, c in game_state['connected'].items() if c]
                if connected and disconnected:
                    winner = connected[0]
                    loser = disconnected[0]
                    try:
                        winner_conn = game_state['conns'][winner]
                        winner_wfile = EncryptedWFileWrapper(winner_conn.makefile('w')) 
                        winner_wfile.write("OPPONENT_TIMEOUT. You win!\n")
                        winner_wfile.flush()
                    except Exception:
                        pass
                    print(f"[INFO] Player {loser} did not reconnect in time. {winner} wins by forfeit.")
                # --- Mark the game as finished so no further reconnects are allowed ---
                game_state['waiting_reconnect'] = False
                del games[game_key]
            else:
                # Game finished normally, remove game
                del games[game_key]
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
                # Broadcast to all lobby clients, including their queue position
                for idx, (conn, addr, username) in enumerate(waiting_lines):
                    try:
                        lobby_wfile = EncryptedWFileWrapper(conn.makefile('w'))
                        pos_msg = f"{msg}\n[LOBBY] You are position {idx+1} in the queue."
                        lobby_wfile.write(pos_msg + "\n")
                        lobby_wfile.flush()
                    except Exception:
                        pass
                time.sleep(5.0)
                (conn1, addr1, username1) = waiting_lines.pop(0)
                (conn2, addr2, username2) = waiting_lines.pop(0)
                print("[INFO] Starting new two player game.")
                threading.Thread(target=two_player_game, args=(conn1, addr1, conn2, addr2, username1, username2), daemon=True).start()
        threading.Event().wait(0.5) # Sleeps the threaded game if no players

def game_manager(conn, addr, mode):
    username, conn, addr = handle_initial_connection(conn, addr)
    if not username:
        return
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
                    wfile = EncryptedWFileWrapper(conn.makefile('w'))
                    wfile.write("A game is currently in progress. You are in the lobby and will join the next game when it starts.\n")
                    wfile.flush()
                except Exception as e:
                    print(f"[WARN] Failed to notify player at {addr}: {e}")
            else:
                try:
                    wfile = EncryptedWFileWrapper(conn.makefile('w'))
                    wfile.write("Waiting for another player to join...\n")
                    wfile.flush()
                except Exception as e:
                    print(f"[WARN] Failed to notify player at {addr}: {e}")
        # Wait for the connection to close (i.e., after a game or disconnect)
        try:
            while True:
                if conn.fileno() == -1:
                    # Only print if the player is still in the lobby (not matched into a game)
                    with waiting_players_lock:
                        if (conn, addr) in waiting_lines:
                            print(f"[INFO] Player at {addr} QUIT or disconnected while in the lobby.")
                            waiting_lines.remove((conn, addr))
                    break
                with waiting_players_lock:
                    # Only print if the player is still in the lobby (not matched into a game)
                    if (conn, addr) not in waiting_lines and conn.fileno() != -1:
                        break
                time.sleep(0.5)
        except Exception:
            pass

def main():
    mode = input ("Select mode: (1) Single player, (2) Two player: ").strip()
    print(f"[INFO] Server listening on {HOST}:{PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, PORT))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.listen(10)
        s.settimeout(1.0)
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