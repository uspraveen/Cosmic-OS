import requests

# 1. The number you want to message (Country Code + Number)
# Example: If your number is +1 (555) 123-4567, use "15551234567"
TARGET_NUMBER = "919003535237" 

# 2. The message you want to send
MESSAGE_BODY = "Arun, You have a meeting in 15mins. Is there anything you want me to do about it?"

# 3. Send the request to your Node.js Gateway
try:
    response = requests.post("http://localhost:3000/send-message", json={
        "number": TARGET_NUMBER,
        "message": MESSAGE_BODY
    })
    
    if response.status_code == 200:
        print("✅ Message sent successfully!")
    else:
        print(f"❌ Failed: {response.text}")
        
except Exception as e:
    print(f"❌ Error: {e}")
    print("Make sure 'node gateway.js' is running in another window!")