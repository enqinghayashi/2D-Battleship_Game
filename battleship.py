"""
battleship.py

Contains core data structures and logic for Battleship, including:
 - Board class for storing ship positions, hits, misses
 - Utility function parse_coordinate for translating e.g. 'B5' -> (row, col)
 - A test harness run_single_player_game() to demonstrate the logic in a local, single-player mode

"""

import random
import time

BOARD_SIZE = 10
SHIPS = [
    ("Carrier", 5),
    ("Battleship", 4),
    ("Cruiser", 3),
    ("Submarine", 3),
    ("Destroyer", 2)
]


class Board:
    """
    Represents a single Battleship board with hidden ships.
    We store:
      - self.hidden_grid: tracks real positions of ships ('S'), hits ('X'), misses ('o')
      - self.display_grid: the version we show to the player ('.' for unknown, 'X' for hits, 'o' for misses)
      - self.placed_ships: a list of dicts, each dict with:
          {
             'name': <ship_name>,
             'positions': set of (r, c),
          }
        used to determine when a specific ship has been fully sunk.

    In a full 2-player networked game:
      - Each player has their own Board instance.
      - When a player fires at their opponent, the server calls
        opponent_board.fire_at(...) and sends back the result.
    """

    def __init__(self, size=BOARD_SIZE):
        self.size = size
        # '.' for empty water
        self.hidden_grid = [['.' for _ in range(size)] for _ in range(size)]
        # display_grid is what the player or an observer sees (no 'S')
        self.display_grid = [['.' for _ in range(size)] for _ in range(size)]
        self.placed_ships = []  # e.g. [{'name': 'Destroyer', 'positions': {(r, c), ...}}, ...]

    def place_ships_randomly(self, ships=SHIPS):
        """
        Randomly place each ship in 'ships' on the hidden_grid, storing positions for each ship.
        In a networked version, you might parse explicit placements from a player's commands
        (e.g. "PLACE A1 H BATTLESHIP") or prompt the user for board coordinates and placement orientations; 
        the self.place_ships_manually() can be used as a guide.
        """
        for ship_name, ship_size in ships:
            placed = False
            while not placed:
                orientation = random.randint(0, 1)  # 0 => horizontal, 1 => vertical
                row = random.randint(0, self.size - 1)
                col = random.randint(0, self.size - 1)

                if self.can_place_ship(row, col, ship_size, orientation):
                    occupied_positions = self.do_place_ship(row, col, ship_size, orientation)
                    self.placed_ships.append({
                        'name': ship_name,
                        'positions': occupied_positions
                    })
                    placed = True


    def place_ships_manually(self, ships=SHIPS):
        """
        Prompt the user for each ship's starting coordinate and orientation (H or V).
        Validates the placement; if invalid, re-prompts.
        """
        print("\nPlease place your ships manually on the board.")
        for ship_name, ship_size in ships:
            while True:
                self.print_display_grid(show_hidden_board=True)
                print(f"\nPlacing your {ship_name} (size {ship_size}).")
                coord_str = input("  Enter starting coordinate (e.g. A1): ").strip()
                orientation_str = input("  Orientation? Enter 'H' (horizontal) or 'V' (vertical): ").strip().upper()

                try:
                    row, col = parse_coordinate(coord_str)
                except ValueError as e:
                    print(f"  [!] Invalid coordinate: {e}")
                    continue

                # Convert orientation_str to 0 (horizontal) or 1 (vertical)
                if orientation_str == 'H':
                    orientation = 0
                elif orientation_str == 'V':
                    orientation = 1
                else:
                    print("  [!] Invalid orientation. Please enter 'H' or 'V'.")
                    continue

                # Check if we can place the ship
                if self.can_place_ship(row, col, ship_size, orientation):
                    occupied_positions = self.do_place_ship(row, col, ship_size, orientation)
                    self.placed_ships.append({
                        'name': ship_name,
                        'positions': occupied_positions
                    })
                    break
                else:
                    print(f"  [!] Cannot place {ship_name} at {coord_str} (orientation={orientation_str}). Try again.")


    def can_place_ship(self, row, col, ship_size, orientation):
        """
        Check if we can place a ship of length 'ship_size' at (row, col)
        with the given orientation (0 => horizontal, 1 => vertical).
        Returns True if the space is free, False otherwise.
        """
        if orientation == 0:  # Horizontal
            if col + ship_size > self.size:
                return False
            for c in range(col, col + ship_size):
                if self.hidden_grid[row][c] != '.':
                    return False
        else:  # Vertical
            if row + ship_size > self.size:
                return False
            for r in range(row, row + ship_size):
                if self.hidden_grid[r][col] != '.':
                    return False
        return True

    def do_place_ship(self, row, col, ship_size, orientation):
        """
        Place the ship on hidden_grid by marking 'S', and return the set of occupied positions.
        """
        occupied = set()
        if orientation == 0:  # Horizontal
            for c in range(col, col + ship_size):
                self.hidden_grid[row][c] = 'S'
                occupied.add((row, c))
        else:  # Vertical
            for r in range(row, row + ship_size):
                self.hidden_grid[r][col] = 'S'
                occupied.add((r, col))
        return occupied

    def fire_at(self, row, col):
        """
        Fire at (row, col). Return a tuple (result, sunk_ship_name).
        Possible outcomes:
          - ('hit', None)          if it's a hit but not sunk
          - ('hit', <ship_name>)   if that shot causes the entire ship to sink
          - ('miss', None)         if no ship was there
          - ('already_shot', None) if that cell was already revealed as 'X' or 'o'

        The server can use this result to inform the firing player.
        """
        cell = self.hidden_grid[row][col]
        if cell == 'S':
            # Mark a hit
            self.hidden_grid[row][col] = 'X'
            self.display_grid[row][col] = 'X'
            # Check if that hit sank a ship
            sunk_ship_name = self._mark_hit_and_check_sunk(row, col)
            if sunk_ship_name:
                return ('hit', sunk_ship_name)  # A ship has just been sunk
            else:
                return ('hit', None)
        elif cell == '.':
            # Mark a miss
            self.hidden_grid[row][col] = 'o'
            self.display_grid[row][col] = 'o'
            return ('miss', None)
        elif cell == 'X' or cell == 'o':
            return ('already_shot', None)
        else:
            # In principle, this branch shouldn't happen if 'S', '.', 'X', 'o' are all possibilities
            return ('already_shot', None)

    def _mark_hit_and_check_sunk(self, row, col):
        """
        Remove (row, col) from the relevant ship's positions.
        If that ship's positions become empty, return the ship name (it's sunk).
        Otherwise return None.
        """
        for ship in self.placed_ships:
            if (row, col) in ship['positions']:
                ship['positions'].remove((row, col))
                if len(ship['positions']) == 0:
                    return ship['name']
                break
        return None

    def all_ships_sunk(self):
        """
        Check if all ships are sunk (i.e. every ship's positions are empty).
        """
        for ship in self.placed_ships:
            if len(ship['positions']) > 0:
                return False
        return True

    def print_display_grid(self, show_hidden_board=False):
        """
        Print the board as a 2D grid.
        
        If show_hidden_board is False (default), it prints the 'attacker' or 'observer' view:
        - '.' for unknown cells,
        - 'X' for known hits,
        - 'o' for known misses.
        
        If show_hidden_board is True, it prints the entire hidden grid:
        - 'S' for ships,
        - 'X' for hits,
        - 'o' for misses,
        - '.' for empty water.
        """
        # Decide which grid to print
        grid_to_print = self.hidden_grid if show_hidden_board else self.display_grid

        # Column headers (1 .. N)
        print("  " + "".join(str(i + 1).rjust(2) for i in range(self.size)))
        # Each row labeled with A, B, C, ...
        for r in range(self.size):
            row_label = chr(ord('A') + r)
            row_str = " ".join(grid_to_print[r][c] for c in range(self.size))
            print(f"{row_label:2} {row_str}")

def parse_coordinate(coord_str):
    """
    Convert something like 'B5' into zero-based (row, col).
    Example: 'A1' => (0, 0), 'C10' => (2, 9)
    HINT: you might want to add additional input validation here...
    """
    coord_str = coord_str.strip().upper()

    if len(coord_str) < 2:
        raise ValueError("Coordinate too short")

    row_letter = coord_str[0]
    col_digits = coord_str[1:]

    if not row_letter.isalpha() or not col_digits.isdigit():
        raise ValueError("Invalid coordinate format")

    row = ord(row_letter) - ord('A')
    col = int(col_digits) - 1  # zero-based

    if row < 0 or row >= BOARD_SIZE or col < 0 or col >= BOARD_SIZE:
        raise ValueError(f"Coordinate out of bounds: {coord_str}")

    return (row, col)


def format_result_message(result, sunk_name=None):
    """
    Format the result of a FIRE command as a protocol message.
    """
    if result == 'hit':
        if sunk_name:
            return f"RESULT HIT SUNK {sunk_name.upper()}"
        else:
            return "RESULT HIT"
    elif result == 'miss':
        return "RESULT MISS"
    elif result == 'already_shot':
        return "RESULT ALREADY_SHOT"
    else:
        return "RESULT ERROR"

def parse_fire_message(msg):
    """
    Parse a FIRE message, e.g. 'FIRE B5' -> ('FIRE', 'B5')
    """
    parts = msg.strip().split()
    if len(parts) == 2 and parts[0].upper() == 'FIRE':
        return parts[1]
    raise ValueError("Invalid FIRE message format")

def parse_place_message(msg):
    """
    Parse a PLACE message, e.g. 'PLACE A1 H BATTLESHIP'
    """
    parts = msg.strip().split()
    if len(parts) == 4 and parts[0].upper() == 'PLACE':
        return parts[1], parts[2].upper(), parts[3].upper()
    raise ValueError("Invalid PLACE message format")


def run_single_player_game_locally():
    """
    A test harness for local single-player mode, demonstrating two approaches:
     1) place_ships_manually()
     2) place_ships_randomly()

    Then the player tries to sink them by firing coordinates.
    """
    board = Board(BOARD_SIZE)

    # Ask user how they'd like to place ships
    choice = input("Place ships manually (M) or randomly (R)? [M/R]: ").strip().upper()
    if choice == 'M':
        board.place_ships_manually(SHIPS)
    else:
        board.place_ships_randomly(SHIPS)

    print("\nNow try to sink all the ships!")
    moves = 0
    while True:
        board.print_display_grid()
        guess = input("\nEnter coordinate to fire at (or 'quit'): ").strip()
        if guess.lower() == 'quit':
            print("Thanks for playing. Exiting...")
            return

        try:
            row, col = parse_coordinate(guess)
            result, sunk_name = board.fire_at(row, col)
            moves += 1

            if result == 'hit':
                if sunk_name:
                    print(f"  >> HIT! You sank the {sunk_name}!")
                else:
                    print("  >> HIT!")
                if board.all_ships_sunk():
                    board.print_display_grid()
                    print(f"\nCongratulations! You sank all ships in {moves} moves.")
                    break
            elif result == 'miss':
                print("  >> MISS!")
            elif result == 'already_shot':
                print("  >> You've already fired at that location. Try again.")

        except ValueError as e:
            print("  >> Invalid input:", e)


def run_single_player_game_online(rfile, wfile):
    """
    A test harness for running the single-player game with I/O redirected to socket file objects.
    Uses minimal protocol messages: FIRE <coord>, RESULT <result>, etc.
    """
    def send(msg):
        try:
            wfile.write(msg + '\n')
            wfile.flush()
        except Exception:
            raise ConnectionError("Player disconnected from the game") 
    def send_board(board):
        try:
            wfile.write("GRID\n")
            wfile.write("  " + " ".join(str(i + 1).rjust(2) for i in range(board.size)) + '\n')
            for r in range(board.size):
                row_label = chr(ord('A') + r)
                row_str = " ".join(board.display_grid[r][c] for c in range(board.size))
                wfile.write(f"{row_label:2} {row_str}\n")
            wfile.write('\n')
            wfile.flush()
        except Exception:
            raise ConnectionError("Player disconnected from the game") 
        
    def recv():
        try:
            if rfile.readline().strip():
                return rfile.readline().strip()
        except Exception:
            raise ConnectionError("Player disconnected from the game")

    board = Board(BOARD_SIZE)
    board.place_ships_randomly(SHIPS)

    send("WELCOME")
    moves = 0
    while True:
        send_board(board)
        send("READY")  # Prompt client to FIRE
        msg = recv()
        if msg.lower() == 'quit':
            send("BYE")
            return
        try:
            coord = parse_fire_message(msg)
            row, col = parse_coordinate(coord)
            result, sunk_name = board.fire_at(row, col)
            moves += 1
            send(format_result_message(result, sunk_name))
            if result == 'hit' and board.all_ships_sunk():
                send_board(board)
                send(f"WIN {moves}")
                return
        except Exception as e:
            send(f"ERROR {e}")


def run_two_player_game_online(
    rfile1, wfile1, rfile2, wfile2,
    spectator_msg_callback=None, spectator_board_callback=None,
    two_player_sessions=None, two_player_sessions_lock=None,
    username1=None, username2=None, RECONNECT_TIMEOUT=60
):
    """
    Runs a two-player online Battleship game.
    Each player places ships, then takes turns firing at the other.
    Reports hit/miss/sunk, ends when one player has all ships sunk or forfeits.
    Uses minimal protocol messages: PLACE, FIRE, RESULT, WIN, etc.
    If spectator_msg_callback and spectator_board_callback are provided,
    notify updates to spectators.
    Enables reconnection support.
    """
    def send(wfile, msg):
        try:
            wfile.write(msg + '\n')
            wfile.flush()
        except Exception:
            raise ConnectionError("Opponent disconnected from the game")
    def send_my_board(wfile, board):
        try:
            wfile.write("OWN_BOARD\n")
            wfile.write("   " + " ".join(f"{i+1:2}" for i in range(board.size)) + '\n')
            for r in range(board.size):
                row_label = chr(ord('A') + r)
                row_str = " ".join(board.hidden_grid[r][c] for c in range(board.size))
                wfile.write(f"{row_label:2} {row_str}\n")
            wfile.write('\n')
            wfile.flush()
        except Exception:
            raise ConnectionError("Opponent disconnected from the game")

    def send_board(wfile, board):
        try:
            wfile.write("GRID\n")
            wfile.write("   " + " ".join(f"{i+1:2}" for i in range(board.size)) + '\n')
            for r in range(board.size):
                row_label = chr(ord('A') + r)
                row_str = " ".join(board.display_grid[r][c] for c in range(board.size))
                wfile.write(f"{row_label:2} {row_str}\n")
            wfile.write('\n')
            wfile.flush()
        except Exception:
            raise ConnectionError("Opponent disconnected from the game")

    def wait_for_reconnect(username, opponent_username, timeout=RECONNECT_TIMEOUT):
        start = time.time()
        while time.time() - start < timeout:
            with two_player_sessions_lock:
                session = two_player_sessions.get(username)
                if session and session.get('conn'):
                    return session['conn'], session['rfile'], session['wfile']
            time.sleep(1)
        return None, None, None

    def safe_recv(rfile, username=None, opponent_username=None):
        try:
            line = rfile.readline()
            if not line and two_player_sessions and username and opponent_username:
                send(two_player_sessions[opponent_username]['wfile'], f"WAITING_FOR_RECONNECT {username}")
                conn, new_rfile, new_wfile = wait_for_reconnect(username, opponent_username)
                if not conn:
                    send(two_player_sessions[opponent_username]['wfile'], f"RECONNECT_TIMEOUT {username}")
                    return None
                with two_player_sessions_lock:
                    two_player_sessions[username]['rfile'] = new_rfile
                    two_player_sessions[username]['wfile'] = new_wfile
                return safe_recv(new_rfile, username, opponent_username)
            elif not line:
                raise ConnectionError("Opponent disconnected from the game")
            return line.strip()
        except Exception:
            return None

    board1 = Board(BOARD_SIZE)
    board2 = Board(BOARD_SIZE)
    send(wfile1, "WELCOME PLAYER 1")
    send(wfile2, "WELCOME PLAYER 2")
    send(wfile1, "PLACE_SHIPS")
    send(wfile2, "PLACE_SHIPS")

    for board, rfile, wfile, player_num in [
        (board1, rfile1, wfile1, 1),
        (board2, rfile2, wfile2, 2)
    ]:
        for ship_name, ship_size in SHIPS:
            while True:
                send(wfile, f"PLACE {ship_name}(shipName) {ship_size}(shipSize) ")
                send(wfile, f"Respond something like PLACE <COORD> <ORIENTATION> <SHIPNAME> ")
                send(wfile, f"e.g. 'place b6 v battleship' v:vertical, h: horizontal ")
                if two_player_sessions and two_player_sessions_lock and username1 and username2:
                    username = username1 if player_num == 1 else username2
                    opponent_username = username2 if player_num == 1 else username1
                    msg = safe_recv(rfile, username, opponent_username)
                else:
                    msg = safe_recv(rfile)
                if msg is None:
                    return
                try:
                    coord_str, orientation_str, name = parse_place_message(msg)
                    if name != ship_name.upper():
                        send(wfile, "ERROR Ship name mismatch")
                        continue
                    row, col = parse_coordinate(coord_str)
                    orientation = 0 if orientation_str == 'H' else 1 if orientation_str == 'V' else None
                    if orientation is None:
                        send(wfile, "ERROR Invalid orientation")
                        continue
                    if board.can_place_ship(row, col, ship_size, orientation):
                        occupied_positions = board.do_place_ship(row, col, ship_size, orientation)
                        board.placed_ships.append({
                            'name': ship_name,
                            'positions': occupied_positions
                        })
                        send(wfile, "PLACED")
                        break
                    else:
                        send(wfile, "ERROR Cannot place ship at that location")
                except Exception as e:
                    send(wfile, f"ERROR {e}")

    send(wfile1, "ALL_SHIPS_PLACED")
    send(wfile2, "ALL_SHIPS_PLACED")

    moves = 0
    turn = 0  # 0 for Player 1's turn, 1 for Player 2's

    while True:
        if turn == 0:
            rfile, wfile = rfile1, wfile1
            opponent_wfile = wfile2
            opponent_board = board2
            player_num = 1
            player_board = board1
            username = username1 if two_player_sessions else None
            opponent_username = username2 if two_player_sessions else None
        else:
            rfile, wfile = rfile2, wfile2
            opponent_wfile = wfile1
            opponent_board = board1
            player_num = 2
            player_board = board2
            username = username2 if two_player_sessions else None
            opponent_username = username1 if two_player_sessions else None

        send_my_board(wfile, player_board)
        send_board(wfile, opponent_board)
        send(wfile, "READY")
        send(opponent_wfile, "WAITING")
        if spectator_msg_callback:
            spectator_msg_callback(f"[INFO] Player {player_num}'s turn.")
        if spectator_board_callback:
            spectator_board_callback(board1, label="PLAYER1_GRID")
            spectator_board_callback(board2, label="PLAYER2_GRID")

        if two_player_sessions and two_player_sessions_lock and username and opponent_username:
            msg = safe_recv(rfile, username, opponent_username)
        else:
            msg = safe_recv(rfile)
        if msg is None:
            send(opponent_wfile, "OPPONENT_DISCONNECTED. YOU WIN!\n")
            return
        if msg.lower() == 'quit':
            send(wfile, "BYE")
            send(opponent_wfile, "OPPONENT_QUIT")
            if spectator_msg_callback:
                spectator_msg_callback("[INFO] A player quit. Game ended.")
            break

        try:
            coord = parse_fire_message(msg)
            if len(coord) < 2 or not coord[0].isalpha() or not coord[1:].isdigit():
                send(wfile, f"ERROR Invalid coordinate format: {coord}")
                continue

            row, col = parse_coordinate(coord)
            if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
                send(wfile, f"ERROR Coordinate out of range: {coord}")
                continue
            
            result, sunk_name = opponent_board.fire_at(row, col)       
            moves += 1
            msg_result = format_result_message(result, sunk_name)
            send(wfile, msg_result)
            if result == 'hit':
                if sunk_name:
                    send(wfile, f"SUNK {sunk_name.upper()}")
                    send(opponent_wfile, f"YOUR_SHIP_SUNK {sunk_name.upper()}")
                    if spectator_msg_callback:
                        spectator_msg_callback(f"[UPDATE] {sunk_name.upper()} was sunk!")
                else:
                    send(wfile, "HIT")
                    send(opponent_wfile, "YOUR_SHIP_HIT")
                    if spectator_msg_callback:
                        spectator_msg_callback(f"[UPDATE] It was a hit!")
                if opponent_board.all_ships_sunk():
                    send_board(wfile, opponent_board)
                    send_board(opponent_wfile, opponent_board)
                    send(wfile, f"WIN {moves}")
                    send(opponent_wfile, "LOSE")
                    if spectator_msg_callback:
                        spectator_msg_callback(f"[GAME OVER] Player {player_num} wins in {moves} moves!")
                    if spectator_board_callback:
                        spectator_board_callback(board1, label="PLAYER1_GRID")
                        spectator_board_callback(board2, label="PLAYER2_GRID")
                    break
            elif result == 'miss':
                send(wfile, "MISS")
                send(opponent_wfile, "OPPONENT_MISS")
            elif result == 'already_shot':
                send(wfile, "ALREADY_SHOT")
                continue
            # Switch turns for players
            turn = 1 - turn
        except Exception as e:
            send(wfile, f"ERROR {e}")
            continue


if __name__ == "__main__":
    # Optional: run this file as a script to test single-player mode
    run_single_player_game_locally()
