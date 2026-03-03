import os

import requests

TARGET_NUMBER = "919003535237"
MESSAGE_BODY = "Arun, You have a meeting in 15mins. Is there anything you want me to do about it?"
BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:3000/send-message")
BRIDGE_TOKEN = os.environ.get("WHATSAPP_BRIDGE_TOKEN", "")

headers = {}
if BRIDGE_TOKEN:
    headers["X-Bridge-Token"] = BRIDGE_TOKEN

try:
    response = requests.post(
        BRIDGE_URL,
        json={"number": TARGET_NUMBER, "message": MESSAGE_BODY},
        headers=headers,
        timeout=30,
    )

    if response.status_code == 200:
        print("Message sent successfully.")
    else:
        print(f"Failed: {response.status_code} {response.text}")

except Exception as exc:
    print(f"Error: {exc}")
    print("Make sure the Backend WhatsApp bridge is running.")
