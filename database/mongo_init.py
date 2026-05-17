"""
Run this script to manually seed the MongoDB database.
Usage: python database/mongo_init.py
"""
from pymongo import MongoClient
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "tickethub")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

# Create indexes
db.users.create_index("email", unique=True)
print("✓ Indexes created")

# Seed tickets if empty
if db.tickets.count_documents({}) == 0:
    db.tickets.insert_many([
        {"type":"BUS",     "title":"Delhi to Jaipur Volvo AC",  "source":"Delhi",     "destination":"Jaipur",    "price":800,  "discount":10,"total_seats":40,  "created_at":datetime.now()},
        {"type":"TRAIN",   "title":"Patna Rajdhani Express",     "source":"Patna",     "destination":"Delhi",     "price":1200, "discount":5, "total_seats":60,  "created_at":datetime.now()},
        {"type":"FLIGHT",  "title":"IndiGo 6E-203",              "source":"Delhi",     "destination":"Mumbai",    "price":4500, "discount":15,"total_seats":180, "created_at":datetime.now()},
        {"type":"CONCERT", "title":"Arijit Singh Live Mumbai",   "source":"Mumbai",    "destination":"Mumbai",    "price":3000, "discount":20,"total_seats":500, "created_at":datetime.now()},
        {"type":"HOTEL",   "title":"Taj Hotel Mumbai",           "source":"Mumbai",    "destination":"Mumbai",    "price":6000, "discount":10,"total_seats":100, "created_at":datetime.now()},
        {"type":"BUS",     "title":"Mumbai to Goa Sleeper",      "source":"Mumbai",    "destination":"Goa",       "price":1200, "discount":0, "total_seats":40,  "created_at":datetime.now()},
        {"type":"TRAIN",   "title":"Bangalore-Chennai Express",  "source":"Bangalore", "destination":"Chennai",   "price":950,  "discount":5, "total_seats":60,  "created_at":datetime.now()},
        {"type":"FLIGHT",  "title":"Air India AI-101",           "source":"Delhi",     "destination":"Bangalore", "price":3800, "discount":10,"total_seats":150, "created_at":datetime.now()},
    ])
    print(f"✓ Seeded 8 sample tickets")
else:
    print(f"ℹ  Tickets already exist, skipping seed")

print("✓ MongoDB initialized successfully!")
client.close()
