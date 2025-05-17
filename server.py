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
        run_single_player_game_online(rfile, wfile)
    except Exception as e:
        print(f"[WARN] Single player client {addr} disconnected: {e}")
    finally:
        conn.close()
        print(f"[INFO] Single player client {addr} connection closed.")

def two_player_game(conn1, addr1, conn2, addr2):
    global game_running
    winner = None
    loser = None
    winner_reason = None  # "timeout", "disconnect", "win"
    try:
        rfile1 = conn1.makefile('r')
        wfile1 = conn1.makefile('w')
        rfile2 = conn2.makefile('r')
        wfile2 = conn2.makefile('w')
        game_running.set()
        # Wrap the game logic to detect who won/lost and why
        try:
            run_two_player_game_online(rfile1, wfile1, rfile2, wfile2)
        except Exception as e:
            print(f"[ERROR] Exception during game logic: {e}")
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
        print(f"[INFO] Notified remaining player(s) of win and returning to lobby.")
    finally:
        try:
            # Determine who is still connected
            still_connected = []
            disconnected = []
            for conn, addr in [(conn1, addr1), (conn2, addr2)]:
                try:
                    conn.setblocking(False)
                    try:
                        data = conn.recv(1, socket.MSG_PEEK)
                        still_connected.append((conn, addr))
                    except Exception:
                        disconnected.append((conn, addr))
                except Exception:
                    disconnected.append((conn, addr))
            # Restore blocking mode for any still connected
            for conn, _ in still_connected:
                try: conn.setblocking(True)
                except Exception: pass

            # Determine winner/loser and reason
            # If only one is still connected, that player is the winner by disconnect/timeout
            if len(still_connected) == 1 and len(disconnected) == 1:
                winner_conn, winner_addr = still_connected[0]
                loser_conn, loser_addr = disconnected[0]
                winner = (winner_conn, winner_addr)
                loser = (loser_conn, loser_addr)
                winner_reason = "timeout/disconnect"
                print(f"[INFO] Player at {winner_addr} WON (opponent timeout/disconnect, waiting for next match or Ctrl+C to exit).")
                print(f"[INFO] Player at {loser_addr} QUIT or disconnected during the game.")
                # Winner gets priority: insert at front of lobby
                with waiting_players_lock:
                    waiting_lines.insert(0, (winner_conn, winner_addr))
                try: loser_conn.close()
                except Exception: pass
            # If both are still connected, the game ended normally (one lost by all ships sunk)
            elif len(still_connected) == 2:
                # Ask both clients who won/lost by reading their last message (WIN/LOSE)
                # But since the game logic already sends WIN/LOSE, we just treat both as still connected
                # The winner is the one who did NOT receive "LOSE"
                # For simplicity, push both to lobby, but loser at end, winner at front
                print(f"[INFO] Both players at {addr1} and {addr2} are still connected, game ended normally.")
                # Insert both, winner at front, loser at end
                # Try to read from their sockets to see who is the winner
                # (This is a best-effort guess, as the protocol is not strictly stateful)
                try:
                    conn1.setblocking(False)
                    msg1 = conn1.recv(4096, socket.MSG_PEEK).decode(errors="ignore")
                except Exception:
                    msg1 = ""
                try:
                    conn2.setblocking(False)
                    msg2 = conn2.recv(4096, socket.MSG_PEEK).decode(errors="ignore")
                except Exception:
                    msg2 = ""
                try: conn1.setblocking(True)
                except Exception: pass
                try: conn2.setblocking(True)
                except Exception: pass

                # Heuristic: if one has "WIN" and the other has "LOSE", use that
                if "WIN" in msg1 and "LOSE" in msg2:
                    winner_conn, winner_addr = conn1, addr1
                    loser_conn, loser_addr = conn2, addr2
                elif "WIN" in msg2 and "LOSE" in msg1:
                    winner_conn, winner_addr = conn2, addr2
                    loser_conn, loser_addr = conn1, addr1
                else:
                    # Fallback: just use FIFO, but winner at front
                    winner_conn, winner_addr = conn1, addr1
                    loser_conn, loser_addr = conn2, addr2

                print(f"[INFO] Player at {winner_addr} WON (all ships sunk, waiting for next match or Ctrl+C to exit).")
                print(f"[INFO] Player at {loser_addr} LOST (all ships sunk, added to end of lobby).")
                with waiting_players_lock:
                    waiting_lines.insert(0, (winner_conn, winner_addr))
                    waiting_lines.append((loser_conn, loser_addr))
            elif len(still_connected) == 0 and len(disconnected) == 2:
                print(f"[INFO] Both players at {addr1} and {addr2} QUIT or disconnected during the game.")
                for conn, addr in disconnected:
                    try: conn.close()
                    except Exception: pass
            else:
                for conn, addr in disconnected:
                    print(f"[INFO] Player at {addr} QUIT or disconnected during the game.")
                    try: conn.close()
                    except Exception: pass

            print(f"[INFO] Two-player game between {addr1} and {addr2} ended. Players returned to lobby if still connected.")
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