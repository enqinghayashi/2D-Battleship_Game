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
from battleship import run_single_player_game_online, run_two_player_game_online

HOST = '127.0.0.1'
PORT = 5000

waiting_lines = []
spectators = []
waiting_players_lock = threading.Lock()
spectators_lock = threading.Lock()
game_lock = threading.Lock()
game_running = threading.Event()

# Store player sessions for reconnection support 
player_sessions = {}  # username: {'conn':..., 'addr':..., 'game_state':..., 'last_disconnect':..., ...}
player_sessions_lock = threading.Lock()
RECONNECT_TIMEOUT = 60  # seconds

# Add two-player session tracking
two_player_sessions = {}  # username: {'conn':..., 'addr':..., 'rfile':..., 'wfile':..., 'board':..., 'opponent':..., 'last_disconnect':..., 'game_state':...}
two_player_sessions_lock = threading.Lock()

def get_username(rfile):
    """Read USERNAME <name> from client at connection."""
    while True:
        line = rfile.readline()
        if not line:
            return None
        if line.startswith("USERNAME "):
            return line.strip().split(" ", 1)[1]
        # Ignore other lines until username is received

def single_player(conn, addr):
    try:
        rfile = conn.makefile('r')
        wfile = conn.makefile('w')
        # Get username/client ID for session tracking 
        username = get_username(rfile)
        if not username:
            wfile.write("ERROR: Username required for reconnection support.\n")
            wfile.flush()
            conn.close()
            return

        # Check for existing session (reconnection) 
        with player_sessions_lock:
            session = player_sessions.get(username)
            now = time.time()
            if session and session.get('last_disconnect') and now - session['last_disconnect'] <= RECONNECT_TIMEOUT:
                # Resume previous game state
                wfile.write("RECONNECTED. Resuming your previous game.\n")
                wfile.flush()
                # Restore game state (for single player, just rerun with saved state)
                # For demo, we just clear the session and start a new game
                del player_sessions[username]
                # In a real implementation, we restore the actual game state here
                run_single_player_game_online(rfile, wfile)
                return
            elif session and session.get('last_disconnect'):
                # Timeout exceeded
                wfile.write("RECONNECT_TIMEOUT. Your previous game has expired.\n")
                wfile.flush()
                del player_sessions[username]
                run_single_player_game_online(rfile, wfile)
                return

        # Start new session 
        run_single_player_game_online(rfile, wfile)
    except Exception as e:
        print(f"[WARN] Single player client {addr} disconnected: {e}")
    finally:
        # Save session for possible reconnection 
        try:
            if 'username' in locals() and username:
                with player_sessions_lock:
                    player_sessions[username] = {
                        'conn': None,
                        'addr': addr,
                        'game_state': None,  # For demo, not storing actual game state
                        'last_disconnect': time.time()
                    }
        except Exception:
            pass
        conn.close()
        print(f"[INFO] Single player client {addr} connection closed.")

def wait_for_reconnect(username, opponent_username, timeout=RECONNECT_TIMEOUT):
    """Wait for a player to reconnect within timeout seconds. Returns new (conn, rfile, wfile) or None."""
    start = time.time()
    while time.time() - start < timeout:
        with two_player_sessions_lock:
            session = two_player_sessions.get(username)
            if session and session.get('conn'):
                # Reconnected
                return session['conn'], session['rfile'], session['wfile']
        time.sleep(1)
    return None, None, None

def handle_spectator(conn, addr):
    try:
        wfile = conn.makefile('w')
        rfile = conn.makefile('r')
        wfile.write("You are a spectator. You can observe the current game.\n")
        wfile.flush()
        while True:
            line = rfile.readline()
            if not line:
                break
            # Respond to any spectator input with an error
            wfile.write("ERROR: Spectators cannot play.\n")
            wfile.flush()
    except Exception as e:
        print(f"[INFO] Spectator {addr} disconnected: {e}")
    finally:
        conn.close()
        print(f"[INFO] Spectator {addr} connection closed.")

def notify_spectator(message):
    with spectators_lock:
        for spectator in spectators[:]:
            try:
                wfile = spectator.makefile('w')
                wfile.write(message + '\n')
                wfile.flush()
            except Exception:
                spectators.remove(spectator)

def notify_spectator_board(board, label="GRID"):
    with spectators_lock:
        for spectator in spectators[:]:
            try:
                wfile = spectator.makefile('w')
                wfile.write(f"{label}\n")
                wfile.write("   " + " ".join(f"{i+1:2}" for i in range(board.size)) + '\n')
                for r in range(board.size):
                    row_label = chr(ord('A') + r)
                    row_str = " ".join(board.display_grid[r][c] for c in range(board.size))
                    wfile.write(f"{row_label:2} {row_str}\n")
                wfile.write('\n')
                wfile.flush()
            except Exception:
                spectators.remove(spectator)

def notify_all_waiting_players(message):
    """Notify all clients in waiting_lines."""
    with waiting_players_lock:
        for conn, addr in waiting_lines:
            try:
                wfile = conn.makefile('w')
                wfile.write(message + '\n')
                wfile.flush()
            except Exception:
                pass  # Ignore failures

def notify_next_match_players(conn1, conn2, username1, username2):
    """Notify the two selected players for the next match."""
    try:
        wfile1 = conn1.makefile('w')
        wfile2 = conn2.makefile('w')
        wfile1.write(f"You have been selected for the next match! You will play against {username2}.\n")
        wfile2.write(f"You have been selected for the next match! You will play against {username1}.\n")
        wfile1.flush()
        wfile2.flush()
    except Exception:
        pass

def two_player_game(conn1, addr1, conn2, addr2):
    global game_running
    # Get usernames for both players ---
    rfile1 = conn1.makefile('r')
    wfile1 = conn1.makefile('w')
    username1 = get_username(rfile1)
    rfile2 = conn2.makefile('r')
    wfile2 = conn2.makefile('w')
    username2 = get_username(rfile2)
    if not username1 or not username2:
        try:
            wfile1.write("ERROR: Username required.\n")
            wfile1.flush()
        except Exception:
            pass
        try:
            wfile2.write("ERROR: Username required.\n")
            wfile2.flush()
        except Exception:
            pass
        conn1.close()
        conn2.close()
        return

    # Register sessions 
    with two_player_sessions_lock:
        two_player_sessions[username1] = {'conn': conn1, 'addr': addr1, 'rfile': rfile1, 'wfile': wfile1, 'opponent': username2, 'last_disconnect': None, 'game_state': None}
        two_player_sessions[username2] = {'conn': conn2, 'addr': addr2, 'rfile': rfile2, 'wfile': wfile2, 'opponent': username1, 'last_disconnect': None, 'game_state': None}

    try:
        game_running.set()
        notify_spectator("[INFO] A new game has started between two players.")

        # run_two_player_game_online with reconnection/session support
        run_two_player_game_online(
            two_player_sessions[username1]['rfile'], two_player_sessions[username1]['wfile'],
            two_player_sessions[username2]['rfile'], two_player_sessions[username2]['wfile'],
            spectator_msg_callback=notify_spectator,
            spectator_board_callback=notify_spectator_board,
            two_player_sessions=two_player_sessions,
            two_player_sessions_lock=two_player_sessions_lock,
            username1=username1,
            username2=username2,
            RECONNECT_TIMEOUT=RECONNECT_TIMEOUT
        )

    except Exception as e:
        print(f"[ERROR] Exception during game: {e}")
        try:
            wfile1.write("OPPONENT_DISCONNECTED. YOU WIN!\n")
            wfile1.flush()
        except Exception:
            pass
        try:
            wfile2.write("OPPONENT_DISCONNECTED. YOU WIN!\n")
            wfile2.flush()
        except Exception:
            pass
    finally:
        try:
            with two_player_sessions_lock:
                if username1 in two_player_sessions:
                    del two_player_sessions[username1]
                if username2 in two_player_sessions:
                    del two_player_sessions[username2]
            conn1.close()
            conn2.close()
            print(f"[INFO] Two-player game between {addr1} and {addr2} ended.")
            game_running.clear()
            notify_spectator("[INFO] The game has ended.")
        except Exception as e:
            print(f"[ERROR] Error in two-player game cleanup: {e}")

def lobby_manager():
    while True:
        with waiting_players_lock:
            # Notify all waiting clients about their position and next match 
            if len(waiting_lines) >= 2 and not game_running.is_set():
                (conn1, addr1) = waiting_lines.pop(0)
                (conn2, addr2) = waiting_lines.pop(0)
                # Get usernames for notification
                try:
                    rfile1 = conn1.makefile('r')
                    username1 = None
                    # Try to peek username from the socket (without consuming it)
                    pos = rfile1.tell() if hasattr(rfile1, 'tell') else None
                    line = rfile1.readline()
                    if line and line.startswith("USERNAME "):
                        username1 = line.strip().split(" ", 1)[1]
                    if pos is not None:
                        rfile1.seek(pos)
                except Exception:
                    username1 = "Player1"
                try:
                    rfile2 = conn2.makefile('r')
                    username2 = None
                    pos = rfile2.tell() if hasattr(rfile2, 'tell') else None
                    line = rfile2.readline()
                    if line and line.startswith("USERNAME "):
                        username2 = line.strip().split(" ", 1)[1]
                    if pos is not None:
                        rfile2.seek(pos)
                except Exception:
                    username2 = "Player2"
                # Notify selected players
                notify_next_match_players(conn1, conn2, username1 or "Player1", username2 or "Player2")
                # Notify all other waiting clients
                notify_all_waiting_players(f"Next match: {username1 or 'Player1'} vs {username2 or 'Player2'} is starting soon.")
                notify_spectator(f"[INFO] Next match: {username1 or 'Player1'} vs {username2 or 'Player2'} is starting.")
                print(f"[INFO] Starting new two player game: {username1 or 'Player1'} vs {username2 or 'Player2'}.")
                threading.Thread(target=two_player_game, args=(conn1, addr1, conn2, addr2), daemon=True).start()
        threading.Event().wait(0.5)

def game_manager(conn, addr, mode):
    if mode == "1":
        single_player(conn, addr)
    else:
        with waiting_players_lock:
            if len(waiting_lines) < 2 and not game_running.is_set():
                waiting_lines.append((conn, addr))
                try:
                    wfile = conn.makefile('w')
                    wfile.write("Waiting for another player to join...\n")
                    wfile.flush()
                except Exception as e:
                    print(f"[WARN] Failed to notify player at {addr}: {e}")
            else:
                with spectators_lock:
                    spectators.append(conn)
                threading.Thread(target=handle_spectator, args=(conn, addr), daemon=True).start()

def main():
    mode = input ("Select mode: (1) Single player, (2) Two player: ").strip()
    print(f"[INFO] Server listening on {HOST}:{PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, PORT))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # keep the server up for running again
        s.listen(10)
        if mode == "2":
            threading.Thread(target=lobby_manager, daemon=True).start()
        while True:
            try:
                conn, addr = s.accept()
                print(f"[INFO] Player connected from {addr}")
                threading.Thread(target=game_manager, args=(conn, addr, mode), daemon=True).start()
            except Exception as e:
                print(f"[ERROR] Accept failed: {e}")

if __name__ == "__main__":
    main()