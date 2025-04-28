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

def receive_messages(rfile):
    global messages
    while running:
        line = rfile.readline()
        if not line:
            messages.append("[INFO] Server disconnected.")
            break

        line = line.strip()

        if line == "GRID":
            messages.append("\n[Board]")
            while True:
                board_line = rfile.readline()
                if not board_line or board_line.strip() == "":
                    break
                messages.append(board_line.strip())
        else:
            messages.append(line)
            
def main():
    global running, messages

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        rfile = s.makefile('r')
        wfile = s.makefile('w')

        threading.Thread(target=receive_messages, args=(rfile,), daemon=True).start()

        time.sleep(0.3)
        for m in messages:
            print(m)
        messages.clear()

        try:
            while running:
                while messages:
                    m = messages.pop(0)
                    print(m)
                    
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

# HINT: A better approach would be something like:
#
# def receive_messages(rfile):
#     """Continuously receive and display messages from the server"""
#     while running:
#         line = rfile.readline()
#         if not line:
#             print("[INFO] Server disconnected.")
#             break
#         # Process and display the message
#
# def main():
#     # Set up connection
#     # Start a thread for receiving messages
#     # Main thread handles sending user input

if __name__ == "__main__":
    main()