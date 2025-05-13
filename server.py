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
from battleship import run_single_player_game_online, run_two_player_game_online

HOST = '127.0.0.1'
PORT = 5000

waiting_lines = []
spectators = []
waiting_players_lock = threading.Lock()
spectators_lock = threading.Lock()
game_lock = threading.Lock()
game_running = threading.Event()

def single_player(conn, addr):
    try:
        rfile = conn.makefile('r')
        wfile = conn.makefile('w')
        run_single_player_game_online(rfile, wfile)
    except Exception as e:
        print(f"[WARN] Single player client {addr} disconnected: {e}")
    finally:
        conn.close()
        print(f"[INFO] Single player client {addr} connection closed.")

def two_player_game(conn1, addr1, conn2, addr2):
    global game_running
    try:
        rfile1 = conn1.makefile('r')
        wfile1 = conn1.makefile('w')
        rfile2 = conn2.makefile('r')
        wfile2 = conn2.makefile('w')
        game_running.set()
        run_two_player_game_online(rfile1, wfile1, rfile2, wfile2)
    except Exception as e:
        print(f"[ERROR] Exception during game: {e}")
            # One or both players disconnected
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
            conn1.close()
            conn2.close()
            print(f"[INFO] Two-player game between {addr1} and {addr2} ended.")
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
        # add the new connected players to the waiting line
        with waiting_players_lock:
            waiting_lines.append((conn, addr))
        try:
            wfile = conn.makefile('w')
            wfile.write("Waiting for another player to join...\n")
            wfile.flush()
        except Exception:
            print(f"[WARN] Failed to notify player at {addr}: {e}")

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

# HINT: For multiple clients, you'd need to:
# 1. Accept connections in a loop
# 2. Handle each client in a separate thread
# 3. Import threading and create a handle_client function

if __name__ == "__main__":
    main()