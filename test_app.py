"""
Minimal FastAPI app to test POST methods
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Create a completely separate FastAPI app
app = FastAPI(title="POST Test App")

# Minimal CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

class TestData(BaseModel):
    test: str

@app.get("/")
async def root():
    return {"message": "POST Test App - GET works"}

@app.post("/")
async def root_post():
    return {"message": "POST Test App - POST works"}

@app.post("/json")
async def post_json(data: TestData):
    return {"message": "POST Test App - POST with JSON works", "received": data.test}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
