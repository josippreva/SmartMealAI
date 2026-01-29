import asyncio
import websockets
import json

async def test_websocket():
    uri = "ws://127.0.0.1:8001/ws/assistant/test-user-123"
    print(f"Connecting to {uri}...")
    
    try:
        async with websockets.connect(uri) as websocket:
            print(" Connected!")
            
            # Test 1: Chat Message
            print("\nSending Chat Message...")
            await websocket.send(json.dumps({
                "type": "chat",
                "content": "How can I reduce calories?"
            }))
            
            response = await websocket.recv()
            print(f" Received: {response}")
            
            # Test 2: Ingredient Substitution
            print("\nSending Substitution Request...")
            await websocket.send(json.dumps({
                "type": "substitute",
                "ingredient": "butter"
            }))
            
            response = await websocket.recv()
            print(f" Received: {response}")
            
            print("\n✅ WebSocket Test Passed!")
            
    except Exception as e:
        print(f"\n❌ WebSocket Test Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_websocket())
