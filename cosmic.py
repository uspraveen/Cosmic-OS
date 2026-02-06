from flask import Flask, request, jsonify

app = Flask(__name__)

# Cosmic's "Thinking" Logic
def think(user_id, message_text):
    clean_text = message_text.lower().strip()
    
    if clean_text == "ping":
        return "Pong! 🛰️ (Python backend)"
    elif "status" in clean_text:
        return "✅ Cosmic is operational. Connected via Node.js Gateway."
    else:
        # Connect your AI/LLM here later
        return f"🤖 Cosmic received: {message_text}"

@app.route('/webhook', methods=['POST'])
def receive_message():
    data = request.json
    sender = data.get('sender')
    text = data.get('text')
    
    print(f"📩 Received from {sender}: {text}")
    
    # Generate response
    reply = think(sender, text)
    
    # Send reply back to Node.js immediately
    return jsonify({"reply": reply})

if __name__ == '__main__':
    print("🧠 Cosmic Brain is running on port 5000...")
    app.run(port=5000)