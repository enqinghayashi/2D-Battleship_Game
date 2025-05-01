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
from battleship import run_single_player_game_online, run_two_player_game_online

HOST = '127.0.0.1'
PORT = 5000

def main():
    mode = input ("Select mode: (1) Single player, (2) Two player: ").strip()
    print(f"[INFO] Server listening on {HOST}:{PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, PORT))
        if mode == "1":
            s.listen(1)
            conn, addr = s.accept()
            print(f"[INFO] Client connected from {addr}")
            with conn:
                rfile = conn.makefile('r')
                wfile = conn.makefile('w')
                run_single_player_game_online(rfile, wfile)
            print("[INFO] Client disconnected.")
        else:
            s.listen(2)
            clients = []
            while len(clients) < 2:
                conn, addr = s.accept()
                print(f"[INFO] Client connected from {addr}")
                clients.append(conn)
            rfile1 = clients[0].makefile('r')
            wfile1 = clients[0].makefile('w')
            rfile2 = clients[1].makefile('r')
            wfile2 = clients[1].makefile('w')
            try:
                run_two_player_game_online(rfile1, wfile1, rfile2, wfile2)
            finally:
                for c in clients:
                    c.close()
            print("[INFO] Two-player game ended. Connections closed.")
# HINT: For multiple clients, you'd need to:
# 1. Accept connections in a loop
# 2. Handle each client in a separate thread
# 3. Import threading and create a handle_client function

if __name__ == "__main__":
    main()