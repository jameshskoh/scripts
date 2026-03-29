SERVICE_NAME="slack-weather-bot"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

echo "Uninstalling systemd service: $SERVICE_NAME..."

sudo systemctl stop "$SERVICE_NAME"
sudo systemctl disable "$SERVICE_NAME"
sudo rm -f "$SERVICE_FILE"
sudo systemctl daemon-reload

echo "Removing virtual environment..."
rm -rf .venv

echo "Done! Service '$SERVICE_NAME' has been removed."
