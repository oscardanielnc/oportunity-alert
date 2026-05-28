import dotenv, uvicorn, sys
dotenv.load_dotenv()
sys.path.insert(0, ".")
from api.app import app
uvicorn.run(app, host="0.0.0.0", port=8081, log_level="warning")
