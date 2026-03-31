"""
Smoke test to verify core imports and basic configuration loading.
"""
import sys
import os

def check_env():
    print("Checking environment...")
    print(f"Python version: {sys.version}")
    print(f"Working directory: {os.getcwd()}")

def test_imports():
    print("\nTesting core imports...")
    try:
        import fastapi
        import uvicorn
        import numpy
        import langchain
        import langgraph
        print("✅ All core dependencies are present.")
    except ImportError as e:
        print(f"❌ Missing dependency: {e}")
        return False
    
    try:
        from app.state import AppState
        from app.config import get
        from app.engines.technical import compute_rsi
        print("✅ App components imported successfully.")
    except Exception as e:
        print(f"❌ App import failed: {e}")
        return False
    return True

def test_config():
    print("\nTesting config loading...")
    try:
        from app import config
        # Check if we can load a value
        host = config.get("server.host")
        print(f"✅ Config loaded. Server host: {host}")
        
        api_key = config.get("zai.api_key")
        if api_key == "YOUR_ZAI_API_KEY_HERE" or not api_key:
            print("⚠️  Warning: ZAI API key is not set in config/settings.json")
        else:
            print("✅ ZAI API key is present.")
    except Exception as e:
        print(f"❌ Config test failed: {e}")
        return False
    return True

if __name__ == "__main__":
    check_env()
    success = test_imports() and test_config()
    
    if success:
        print("\n🚀 Smoke test PASSED. System is ready for launch.")
        sys.exit(0)
    else:
        print("\n❌ Smoke test FAILED.")
        sys.exit(1)
