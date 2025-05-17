"""
client.py

Connects to a Battleship server which runs the single-player game.
Simply pipes user input to the server, and prints all server responses.

TODO: Fix the message synchronization issue using concurrency (Tier 1, item 1).
"""

import socket
import threading
import time

HOST = '127.0.0.1'
PORT = 5000
running = True
messages = []

# HINT: The current problem is that the client is reading from the socket,
# then waiting for user input, then reading again. This causes server
# messages to appear out of order.
#
# Consider using Python's threading module to separate the concerns:
# - One thread continuously reads from the socket and displays messages
# - The main thread handles user input and sends it to the server
#
# import threading

# Keep receiving messages and store into messages first
def receive_messages(rfile):
    global messages, running
    while running:
        line = rfile.readline()
        if not line:
            messages.append("[INFO] Server disconnected.")
            running = False
            break

        line = line.strip()
        
        
        if line == "MY_BOARD":
            messages.append("\n[Your Board]")
            while True:
                board_line = rfile.readline()
                if not board_line or board_line.strip() == "":
                    break
                messages.append(board_line.strip())
        
        # Determine if a grid, store the whole grid in messages. 
        if line == "GRID":
            # Begin reading board lines
            messages.append("\n[Board]")
            while True:
                empty_line = rfile.readline()
                if not empty_line or empty_line.strip() == "": #End loop if empty line detected
                    break
                messages.append(empty_line.strip())
        else:
            # Normal message
            messages.append(line)

        # Do NOT break or reset on win/lose/disconnect messages.
        # Just notify the user and keep the client running.
        if any(x in line for x in [
            "YOU WIN", "WIN", "LOSE", "OPPONENT_DISCONNECTED", "OPPONENT_TIMEOUT", "BYE"
        ]):
            messages.append("[INFO] Looking for next match " 
            "or press Ctrl+C or type quit to exit")

def display_messages():
    while running:
        while messages:
            print(messages.pop(0))
        time.sleep(0.05)  # 防止 CPU 占用过高

            
def main():
    global running, messages

    username = input("Enter your username: ").strip()
    if not username:
        print("Username cannot be empty.")
        return

    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((HOST, PORT))
            rfile = s.makefile('r')
            wfile = s.makefile('w')

            # Send username for identification
            wfile.write(f"USERNAME {username}\n")
            wfile.flush()

            print("Connected to server. Waiting for game to start...")
            initial_msg = rfile.readline().strip()
            if initial_msg:
                print(initial_msg)
            
            # Start threading and receive messages.
            running = True
            threading.Thread(target=receive_messages, args=(rfile,), daemon=True).start()
            threading.Thread(target=display_messages, daemon=True).start()

            # waiting for the welcome messages first
            time.sleep(0.3)
            for m in messages:
                print(m)
            messages.clear()

            try:
                while running:
                    time.sleep(0.1)
                    if messages:
                        continue
                    user_input = input(">> ").strip()
                    if not user_input:
                        continue
                    
                    wfile.write(user_input + '\n')
                    wfile.flush()

                    if user_input.lower() == "quit":
                        running = False
                        break

            except KeyboardInterrupt:
                running = False
                print("\n[INFO] Client exiting.")
                break
        # After disconnect/game end, loop back to reconnect and wait for new game
        print("[INFO] Disconnected or game ended. Reconnecting to server...")
        time.sleep(1)

# HINT: A better approach would be something like:s(rfile):
##     """Continuously receive and display messages from the server"""
# def receive_messages(rfile):
#     """Continuously receive and display messages from the server"""         line = rfile.readline()
#     while running:
#         line = rfile.readline()
#         if not line:
#             print("[INFO] Server disconnected.")he message
#             break
#         # Process and display the message
#ection
# def main():es
#     # Set up connection     # Main thread handles sending user input
#     # Start a thread for receiving messages
#     # Main thread handles sending user input:
if __name__ == "__main__":    main()