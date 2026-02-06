from google import genai

client = genai.Client(
    api_key="AIzaS...f8cCTs"
)

prompt = "Do you think a Dynamic Island style UI makes sense on Windows?"

# Stream the response token-by-token
for chunk in client.models.generate_content_stream(
    model="gemini-3-flash-preview",
    contents=prompt,
):
    if chunk.text:
        print(chunk.text, end="", flush=True)
