# Discord Inactivity Prune Bot

A complete Discord bot written in Python using `discord.py` that scans for inactive members, previews them, and prunes them by resetting their roles to `OUTSIDER & UNRANKED`.

## Features
- **Comprehensive Scanning**: Scans all text channels, threads (active and archived), and forum threads.
- **Optimized Performance**: Message history is scanned back exactly 5 days.
- **Preview System**: See exactly who will be pruned before committing.
- **Graceful Error Handling**: Safely handles missing roles, missing permissions, skipped channels, and respects Discord API rate limits.
- **Audit Logging**: Generates a local `prune_log.txt` tracking all prune operations.

## Setup Instructions

### 1. Requirements
- Install Python 3.8 or higher on your Windows PC.
- Open your terminal or command prompt in this directory (`e:\discordbot`).
- Install the required dependencies:
  ```bash
  pip install -r requirements.txt
  ```

### 2. Configure the Discord Developer Portal
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create a new Application and add a Bot.
3. Under the **Bot** tab, enable the following **Privileged Gateway Intents**:
   - **Server Members Intent**: Required to fetch the full member list and roles.
   - **Message Content Intent**: Required to scan message history properly.
4. Go to the **OAuth2 > URL Generator** tab.
5. Select the `bot` and `applications.commands` scopes.
6. Select the following permissions:
   - `Manage Roles` (Required to reassign roles)
   - `Read Message History` (Required to check activity)
   - `View Channels`
7. Copy the generated URL, paste it into your browser, and invite the bot to your server.

### 3. Bot Configuration
1. Open the `.env` file in the project folder.
2. Replace `your_bot_token_here` with the token from your Discord Developer portal.

### 4. Server Configuration
- Ensure a role named exactly `OUTSIDER & UNRANKED` exists in your server.
- Ensure the Bot's role is **higher** in the Server Settings > Roles list than the roles it needs to remove, and higher than the `OUTSIDER & UNRANKED` role.

### 5. Running the Bot
Run the bot directly from your command line:
```bash
python bot.py
```

You should see output similar to:
```text
Logged in as YourBotName (ID: 1234567890)
Bot is ready and slash commands synced.
```

## Commands
- `/inactivityprune` — Scans the server and generates a preview of inactive members.
- `/confirmprune` — Executes the prune operation, removes other roles, applies the target role, and logs the actions.
- `/cancelprune` — Cancels the pending operation and clears the preview memory.
