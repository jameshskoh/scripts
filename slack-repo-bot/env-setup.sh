python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt

# Install systemd service
SERVICE_NAME="slack-repo-bot"
PROJECT_DIR="$PWD"
CURRENT_USER="$USER"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

echo "Installing systemd service: $SERVICE_NAME..."

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Slack Repo Bot
After=network.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python3 -u $PROJECT_DIR/slack_repo_bot.py
Restart=on-failure
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"

echo "Done! Service '$SERVICE_NAME' is now running."
echo "  View logs:   journalctl -u $SERVICE_NAME -f"
echo "  Stop:        sudo systemctl stop $SERVICE_NAME"
echo "  Restart:     sudo systemctl restart $SERVICE_NAME"