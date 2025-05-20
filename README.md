# Battleship Game Project

## Overview
This project implements a networked version of the classic Battleship game, allowing for single-player games against the server or two-player games between clients, with features like chat, game state persistence, and player reconnection.

## Features

*   **Single-Player Mode**: Play Battleship against the server.
*   **Two-Player Mode**: Play against another player.
*   **Lobby System**: Players wait in a lobby until a two-player game can be formed.
*   **Encrypted Communication**: Game and chat messages are encrypted using AES-CTR via a custom protocol.
*   **Chat Functionality**: Players can send chat messages to each other during the game and in the lobby.
*   **Game State Persistence**:
    *   Two-player game states (boards, turn, ship placements) are saved on the server, allowing games to be resumed if players disconnect and reconnect within a timeout period.
    *   Single-player game states (board, ships placed) are saved, allowing a player to resume their game if they disconnect and reconnect.
*   **Player Reconnection**: If a player disconnects, they have a 60-second window to reconnect and resume their game.
*   **Turn Timeouts**: In two-player games, players have 30 seconds to make their move, or they forfeit the game.

## Prerequisites

*   Python 3.x
*   The `cryptography` library for Python.

## Setup

1.  **Clone the repository or download the files.**
2.  **Install the `cryptography` library**:
    Open your terminal or command prompt and run:
    ```bash
    pip install cryptography
    ```
3.  Ensure all Python files (`server.py`, `client.py`, `battleship.py`, `protocol.py`) are in the same directory.

## Running the Game

### 1. Start the Server

*   Open a terminal or command prompt.
*   Navigate to the directory where you saved the files.
*   Run the server script:
    ```bash
    python server.py
    ```
*   The server will then prompt you to select the game mode:
    ```
    Select mode: (1) Single player, (2) Two player:
    ```
    Enter `1` for single-player mode (each client plays their own game against the server) or `2` for two-player mode (clients are paired up for games).

### 2. Start the Client(s)

*   Open a new terminal or command prompt for each player.
*   Navigate to the directory where you saved the files.
*   Run the client script:
    ```bash
    python client.py
    ```
*   The client will prompt you to enter a username:
    ```
    Enter your username:
    ```
    Enter a unique username and press Enter.

*   **For Single-Player Mode**: Each client connects and starts their own game session with the server.
*   **For Two-Player Mode**:
    *   The first client will connect and wait in the lobby.
    *   The second client will connect, and a game will start between the two players after a short countdown.
    *   Subsequent clients will be placed in the lobby to wait for the next available game.

## Gameplay Instructions

Once connected, follow the on-screen prompts and messages from the server.

### Common Commands

*   **Ship Placement**:
    During the ship placement phase, you will be prompted to place each of your ships. The command format is:
    `place <coordinate> <orientation> <ship_name>`
    *   `<coordinate>`: The starting coordinate for the ship (e.g., `A1`, `B5`).
    *   `<orientation>`: `H` for horizontal or `V` for vertical.
    *   `<ship_name>`: The name of the ship you are placing (e.g., `Carrier`, `Battleship`).
    Example: `place A1 H Carrier`

*   **Firing a Shot**:
    When it's your turn to fire, use the command:
    `fire <coordinate>`
    Example: `fire B5`

*   **Chatting**:
    To send a chat message to other players (in two-player mode or lobby):
    `chat <your message>`
    Example: `chat Hello everyone!`

*   **Quitting**:
    To quit the game or leave the lobby:
    `quit`

### Game Flow

1.  **Connection**: The client connects to the server and sends a username.
2.  **Lobby (Two-Player Mode)**: Players wait until an opponent is available. Chat is enabled in the lobby.
3.  **Ship Placement**: Each player places their ships on their board according to the server's instructions. The server will validate placements.
4.  **Taking Turns (Two-Player Mode)**: Players take turns firing at each other's boards.
5.  **Game End**: The game ends when one player sinks all of their opponent's ships, a player quits, or a player times out.
6.  **Server Messages**: The server will send messages to guide you, such as:
    *   `WELCOME PLAYER 1/2`
    *   `PLACE_SHIPS`
    *   `OWN_BOARD` (your board with your ships)
    *   `GRID` (opponent's board with hits/misses)
    *   `READY` (your turn)
    *   `WAITING` (opponent's turn)
    *   `RESULT HIT/MISS/SUNK <ship_name>`
    *   `TIMEOUT`
    *   `WIN`/`LOSE`
    *   `INFO: ...` (informational messages, e.g., about disconnections)

### Reconnection

If a player disconnects (e.g., closes the client, network issue), they have 60 seconds to restart their client and enter the *same username*. The server will attempt to resume the game from where it left off. If the player fails to reconnect in time, they forfeit the game.

## Protocol

Communication between the client and server uses a custom binary protocol. All packets are encrypted using AES-256 in CTR mode. Each packet includes a sequence number, packet type (game or chat), payload length, a nonce for encryption, the encrypted payload, and a checksum for integrity.

## File Structure

*   `server.py`: Handles client connections, game logic orchestration, and manages game states.
*   `client.py`: Provides the user interface for players to interact with the server.
*   `battleship.py`: Contains the core game logic, including the `Board` class, ship placement, and firing mechanics.
*   `protocol.py`: Defines the network packet structure, encryption, decryption, and checksum functions.
