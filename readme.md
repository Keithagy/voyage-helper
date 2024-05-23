# Astralship voyage tooling repository

Welcome to the source tree for all tools developed to support voyages organized by the [astralship project](https://astralship.org/).

Presently, you may expect to find the following:

- Energy accounting telegram bot + cloud system

# Energy Accounting telegram bot

This bot automates the process of converting voyagers' typed / spoken energy distribution accounts into summarized, bullet-point formats, and then logging them for reporting dataflows + sharing on appropriate channels.

## Deployment view

For this application to work, 2 containers need to be up:

- Python application providing Telegram bot implementation
  - This container's environment-variable dependencies are laid out in the bot's `main` function. If attempting to run this bot locally, please make sure you have the requisite `.env` file from someone already with access to the project.
  - When deployed to the cloud, these environment variables are injected at deploy time via cloud platform configurations (we currently make use of Railway).
- Postgres instance to persist completed energy accounts
  - Locally, we run this as a docker-compose networked stack.
  - On the cloud, the backing database and the telegram bot are set up as separate deployments that have access to each other via private networking.
