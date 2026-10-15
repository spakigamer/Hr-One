import os
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None

async def connect_to_mongo():
    global client, db
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[MONGO_DB]
    print("[INFO] MongoDB connected")
    return client, db


async def close_mongo_connection():
    global client
    if client:
        client.close()
        print("[INFO] MongoDB connection closed")

def get_db()->AsyncIOMotorDatabase:
    if db is None:
        raise RuntimeError("MongoDB not connected")
    return db




