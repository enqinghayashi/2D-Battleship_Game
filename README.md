# CITS3002 BEER Project

## Overview
This is a terminal-based multiplayer **Battleship game** implemented in Python using custom networking protocols, encryption, and real-time messaging. It was built as part of the CITS3002 Computer Networks unit.

## How to Run

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Start the server
```bash
python server.py
```
### 3. Start the Clients (Open Two Terminals)
```bash
python client.py
```
- Enter a username when prompted
- Type mode 2 for multiplayer
- Wait for another player to join the game

### 4. Game Controls
Place ship: place <coordinate> <h for horizontal and v for vertical> <name of the ship>
```bash
place A1 h battleship
```
Fire at enemy: fire <coordinate>
```bash
fire B5
```
Send chat message: chat <message>
```bash
chat Hello
```
Quit game:
```bash
quit
```