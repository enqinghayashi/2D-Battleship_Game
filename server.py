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
waiting_players_lock = threading.Lock() # a lock to thread for needing 2 players to start the game
game_lock = threading.Lock()
game_running = threading.Event()
def single_player(conn, addr):
    try:
        rfile = conn.makefile('r')
        wfile = conn.makefile('w')
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
        print(f"[WARN] Single player client {addr} disconnected: {e}")
    finally:
        conn.close()
        print(f"[INFO] Single player client {addr} connection closed.")

def two_player_game(conn1, addr1, conn2, addr2):
    global game_running
    winner_conn = None
    winner_addr = None
    try:
        rfile1 = conn1.makefile('r')
        wfile1 = conn1.makefile('w')
        rfile2 = conn2.makefile('r')
        wfile2 = conn2.makefile('w')
        print(f"[DEBUG] Starting two player game for {addr1} and {addr2}")
        instruction = (
            "INSTRUCTION: To place a ship, type: place <start_coord> <orientation> <ship_name>\n"
            "Example: place b6 v carrier\n"
        )
        wfile1.write(instruction)
        wfile1.flush()
        wfile2.write(instruction)
        wfile2.flush()
        # Wrap the wfiles to inject instruction on error
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
                for conn, addr in waiting_lines:
                    try:
                        lobby_wfile = conn.makefile('w')
                        lobby_wfile.write(msg + "\n")
                        lobby_wfile.flush()
                    except Exception:
                        pass
        game_running.set()
        try:
            run_two_player_game_online(
                rfile1, WFileWrapper(wfile1),
                rfile2, WFileWrapper(wfile2),
                lobby_broadcast=lobby_broadcast
            )
            print(f"[DEBUG] Finished two player game for {addr1} and {addr2}")
        except Exception as e:
            print(f"[ERROR] Exception during game logic: {e}")
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
        print(f"[INFO] Notified remaining player(s) of win and returning to lobby.")
    finally:
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
                    waiting_lines.insert(0, (conn1, addr1))
                    waiting_lines.append((conn2, addr2))
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

                if len(still_connected) == 1 and len(disconnected) == 1:
                    winner_conn, winner_addr = still_connected[0]
                    quitter_conn, quitter_addr = disconnected[0]
                    print(f"[INFO] Player at {winner_addr} WON (opponent timeout/disconnect, waiting for next match or Ctrl+C to exit).")
                    print(f"[INFO] Player at {quitter_addr} QUIT or disconnected during the game or ship placement.")
                    # --- FIX: Requeue the winner for next match ---
                    with waiting_players_lock:
                        if (winner_conn, winner_addr) not in waiting_lines and winner_conn.fileno() != -1:
                            waiting_lines.insert(0, (winner_conn, winner_addr))
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
                (conn1, addr1) = waiting_lines.pop(0) # extract players from the line by FIFO
                (conn2, addr2) = waiting_lines.pop(0)
                print("[INFO] Starting new two player game.")
                threading.Thread(target=two_player_game, args=(conn1, addr1, conn2, addr2), daemon=True).start()
        threading.Event().wait(0.5) # Sleeps the threaded game if no players

def game_manager(conn, addr, mode):
    if mode == "1":
        single_player(conn, addr)
    else:
        # Only print lobby return message if the player is NOT currently in a game
        with waiting_players_lock:
            waiting_lines.append((conn, addr))
            if game_running.is_set():
                try:
                    wfile = conn.makefile('w')
                    wfile.write("A game is currently in progress. You are in the lobby and will join the next game when it starts.\n")
                    wfile.flush()
                except Exception:
                    print(f"[WARN] Failed to notify player at {addr}: {e}")
            else:
                try:
                    wfile = conn.makefile('w')
                    wfile.write("Waiting for another player to join...\n")
                    wfile.flush()
                except Exception:
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