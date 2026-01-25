from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
import json

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")
router = APIRouter()

class CounterResponse(BaseModel):
    count: int

def get_current_count():
    # This is a placeholder for actual data storage. In a real-world application, you would load the current count from a database or other persistent storage.
    return 0

@router.get("/counter", response_model=CounterResponse)
async def get_counter():
    count = get_current_count()
    return {"count": count}