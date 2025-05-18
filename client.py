"""
client.py

Connects to a Battleship server which runs the single-player game.
Simply pipes user input to the server, and prints all server responses.

TODO: Fix the message synchronization issue using concurrency (Tier 1, item 1).
"""

import socket
import threading
import time
from protocol import build_packet, parse_packet, PKT_TYPE_GAME, PKT_TYPE_CHAT
import struct

HOST = '127.0.0.1'
PORT = 5000
running = True
messages = []

def send_packet(conn, seq, pkt_type, msg):
    payload = msg.encode('utf-8')
    packet = build_packet(seq, pkt_type, payload)
    conn.sendall(packet)

def recv_packet(conn):
    header_size = 7
    header = b''
    while len(header) < header_size:
        chunk = conn.recv(header_size - len(header))
        if not chunk:
            raise ConnectionError("Server disconnected")
        header += chunk
    seq, pkt_type, length = struct.unpack("!IBH", header)
    payload = b''
    while len(payload) < length:
        chunk = conn.recv(length - len(payload))
        if not chunk:
            raise ConnectionError("Server disconnected")
        payload += chunk
    checksum = b''
    while len(checksum) < 4:
        chunk = conn.recv(4 - len(checksum))
        if not chunk:
            raise ConnectionError("Server disconnected")
        checksum += chunk
    packet = header + payload + checksum
    try:
        seq, pkt_type, payload = parse_packet(packet)
        return seq, pkt_type, payload.decode('utf-8')
    except Exception as e:
        return None, None, None

def receive_messages(conn):
    global messages, running
    while running:
        try:
            s, pkt_type, line = recv_packet(conn)
            


            # Handle potential full disconnect or critical parsing error first
            if line is None and pkt_type is None: # Indicates error from parse_packet or true disconnect
                if running: # Avoid appending if already stopped by other means
                    messages.append("[INFO] Server disconnected or sent invalid data.")
                    running = False
                break

            if pkt_type == PKT_TYPE_CHAT and line is not None:
                messages.append(f"[CHAT] {line.strip()}") # Append formatted chat to messages list
                continue
            if line is None:
                messages.append("[INFO] Server disconnected.")
                running = False
                break
            line = line.strip()
            
            if line == "MY_BOARD":
                messages.append("\n[Your Board]")
                while True:
                    s, pkt_type, board_line = recv_packet(conn)
                    if not board_line or board_line.strip() == "":
                        break
                    messages.append(board_line.strip())
            
            if line == "GRID":
                messages.append("\n[Board]")
                while True:
                    s, pkt_type, empty_line = recv_packet(conn)
                    if not empty_line or empty_line.strip() == "":
                        break
                    messages.append(empty_line.strip())
            else:
                messages.append(line)
            if any(x in line for x in [
                "YOU WIN", "WIN", "LOSE", "OPPONENT_DISCONNECTED", "OPPONENT_TIMEOUT", "BYE"
            ]):
                messages.append("[INFO] Looking for next match "
                "or press Ctrl+C or type quit to exit")
        except Exception:
            messages.append("[INFO] Server disconnected.")
            running = False
            break

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
            seq_send = 0
            seq_recv = 0

            # Send username for identification
            send_packet(s, seq_send, PKT_TYPE_GAME, f"USERNAME {username}")
            seq_send += 1

            # Wait for initial server message
            s_, pkt_type, initial_msg = recv_packet(s)
            if initial_msg:
                print(initial_msg.strip())

            running = True
            threading.Thread(target=receive_messages, args=(s,), daemon=True).start()
            threading.Thread(target=display_messages, daemon=True).start()

            time.sleep(0.3)
            for m in messages:
                print(m)
            messages.clear()

            try:
                # --- Always allow user input, even in lobby ---
                while running:
                    user_input = input(">> ").strip()
                    if not user_input:
                        continue
                    if user_input.lower().startswith("chat "):
                        chat_msg = user_input[5:].strip()
                        send_packet(s, seq_send, PKT_TYPE_CHAT, chat_msg)
                        seq_send += 1
                        continue
                    send_packet(s, seq_send, PKT_TYPE_GAME, user_input)
                    seq_send += 1
                    if user_input.lower() == "quit":
                        running = False
                        break
            except KeyboardInterrupt:
                running = False
                print("\n[INFO] Client exiting \n You have been disconnected. If you reconnect within 60 seconds, you can resume the game..")
                break
        print("[INFO] Disconnected or game ended. Reconnecting to server...")
        time.sleep(1)

if __name__ == "__main__":
    main()