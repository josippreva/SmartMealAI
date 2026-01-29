"""
SmartMeal AI - Advanced Recommendation Engine
Includes: Content-based filtering, Collaborative filtering, Calorie calculation, WebSocket AI Assistant
"""
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import pandas as pd
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy.orm import Session
from database import get_db, User, Recipe, Meal, Ingredient
from datetime import datetime, timedelta
import os
import json
from dotenv import load_dotenv
from websocket_assistant import manager, handle_websocket_message

load_dotenv()

app = FastAPI(title="SmartMeal AI API", version="2.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# Pydantic Models
# ============================================

class UserInput(BaseModel):
    user_id: Optional[int] = None
    age: int
    gender: str
    weight: float  # kg
    height: float  # cm
    activity_level: str  # sedentary, light, moderate, active, very_active
    preferences: str
    goals: dict  # {"type": "weight_loss", "target_calories": 1800}
    inventory: list  # dostupne namirnice
    diet_type: Optional[str] = None  # vegan, vegetarian, keto, etc.
    allergies: Optional[List[str]] = []

class RecommendationResponse(BaseModel):
    recommendations: List[Dict]
    daily_calorie_target: int
    macros: Dict[str, float]
    explanation: str

# ============================================
# Helper Functions - Calorie Calculation
# ============================================

def calculate_bmr(age: int, gender: str, weight: float, height: float) -> float:
    """
    Calculate Basal Metabolic Rate using Mifflin-St Jeor Equation
    """
    if gender.lower() == "male":
        bmr = (10 * weight) + (6.25 * height) - (5 * age) + 5
    else:
        bmr = (10 * weight) + (6.25 * height) - (5 * age) - 161
    return bmr

def calculate_tdee(bmr: float, activity_level: str) -> float:
    """
    Calculate Total Daily Energy Expenditure
    """
    activity_multipliers = {
        "sedentary": 1.2,
        "light": 1.375,
        "moderate": 1.55,
        "active": 1.725,
        "very_active": 1.9
    }
    multiplier = activity_multipliers.get(activity_level.lower(), 1.2)
    return bmr * multiplier

def calculate_calorie_target(age: int, gender: str, weight: float, height: float, 
                            activity_level: str, goal_type: str) -> int:
    """
    Calculate daily calorie target based on user goals
    """
    bmr = calculate_bmr(age, gender, weight, height)
    tdee = calculate_tdee(bmr, activity_level)
    
    # Adjust based on goal
    if goal_type == "weight_loss":
        target = tdee - 500  # 500 calorie deficit
    elif goal_type == "muscle_gain":
        target = tdee + 300  # 300 calorie surplus
    else:  # maintenance
        target = tdee
    
    return int(target)

def calculate_macros(calorie_target: int, goal_type: str) -> Dict[str, float]:
    """
    Calculate macro distribution based on goal
    """
    if goal_type == "weight_loss":
        # High protein, moderate carbs, low fat
        protein_ratio = 0.35
        carbs_ratio = 0.40
        fat_ratio = 0.25
    elif goal_type == "muscle_gain":
        # High protein, high carbs, moderate fat
        protein_ratio = 0.30
        carbs_ratio = 0.45
        fat_ratio = 0.25
    else:  # maintenance
        # Balanced
        protein_ratio = 0.25
        carbs_ratio = 0.45
        fat_ratio = 0.30
    
    return {
        "protein": (calorie_target * protein_ratio) / 4,  # 4 cal/g
        "carbs": (calorie_target * carbs_ratio) / 4,     # 4 cal/g
        "fat": (calorie_target * fat_ratio) / 9          # 9 cal/g
    }

# ============================================
# Content-Based Filtering
# ============================================

def content_based_filtering(recipes_df: pd.DataFrame, user_preferences: str, 
                           top_n: int = 10) -> pd.DataFrame:
    """
    Recommend recipes based on content similarity
    """
    if recipes_df.empty:
        return recipes_df
    
    # Create recipe descriptions
    recipes_df['description'] = recipes_df['name'].fillna('') + ' ' + \
                                recipes_df.get('ingredients_text', '').fillna('')
    
    # TF-IDF vectorization
    vectorizer = TfidfVectorizer(stop_words='english', max_features=100)
    
    try:
        recipe_vectors = vectorizer.fit_transform(recipes_df['description'])
        user_vector = vectorizer.transform([user_preferences])
        
        # Calculate cosine similarity
        similarities = cosine_similarity(user_vector, recipe_vectors).flatten()
        recipes_df['content_score'] = similarities
    except:
        recipes_df['content_score'] = 0.0
    
    return recipes_df.nlargest(top_n, 'content_score')

# ============================================
# Collaborative Filtering
# ============================================

def collaborative_filtering(user_id: int, recipes_df: pd.DataFrame, 
                           db: Session, top_n: int = 10) -> pd.DataFrame:
    """
    Recommend recipes based on similar users' preferences
    """
    if recipes_df.empty or not user_id:
        recipes_df['collab_score'] = 0.0
        return recipes_df
    
    # Get user's meal history
    user_meals = db.query(Meal).filter(Meal.user_id == user_id).all()
    user_recipe_ids = set([meal.recipe_id for meal in user_meals])
    
    if not user_recipe_ids:
        recipes_df['collab_score'] = 0.0
        return recipes_df
    
    # Find similar users (users who ate similar recipes)
    similar_users_meals = db.query(Meal).filter(
        Meal.recipe_id.in_(user_recipe_ids),
        Meal.user_id != user_id
    ).all()
    
    # Count recipe popularity among similar users
    recipe_counts = {}
    for meal in similar_users_meals:
        if meal.recipe_id not in user_recipe_ids:  # Don't recommend already eaten
            recipe_counts[meal.recipe_id] = recipe_counts.get(meal.recipe_id, 0) + 1
    
    # Add collaborative score
    recipes_df['collab_score'] = recipes_df['id'].map(
        lambda x: recipe_counts.get(x, 0)
    )
    
    # Normalize scores
    if recipes_df['collab_score'].max() > 0:
        recipes_df['collab_score'] = recipes_df['collab_score'] / recipes_df['collab_score'].max()
    
    return recipes_df

# ============================================
# Main Recommendation Endpoint
# ============================================

@app.post("/recommend-meals/", response_model=RecommendationResponse)
async def recommend_meals(user_input: UserInput, db: Session = Depends(get_db)):
    """
    Advanced meal recommendation system
    """
    try:
        # 1. Calculate calorie target
        goal_type = user_input.goals.get("type", "maintenance")
        calorie_target = calculate_calorie_target(
            user_input.age, user_input.gender, user_input.weight,
            user_input.height, user_input.activity_level, goal_type
        )
        
        # 2. Calculate macro targets
        macros = calculate_macros(calorie_target, goal_type)
        
        # 3. Fetch recipes from database
        recipes_query = db.query(Recipe).all()
        
        if not recipes_query:
            raise HTTPException(status_code=404, detail="No recipes found in database")
        
        # Convert to DataFrame
        recipes_data = []
        for recipe in recipes_query:
            ingredients_text = " ".join([ing.name for ing in recipe.ingredients])
            recipes_data.append({
                "id": recipe.id,
                "name": recipe.name,
                "calories": recipe.calories,
                "protein": recipe.protein,
                "carbs": recipe.carbs,
                "fat": recipe.fat,
                "prep_time": recipe.prep_time,
                "ingredients_text": ingredients_text
            })
        
        recipes_df = pd.DataFrame(recipes_data)
        
        # 4. Filter by diet type
        if user_input.diet_type:
            # This would require additional filtering logic based on ingredients
            pass
        
        # 5. Filter by allergies
        if user_input.allergies:
            # Filter out recipes with allergens
            for recipe in recipes_query:
                ingredient_names = [ing.name.lower() for ing in recipe.ingredients]
                has_allergen = any(allergen.lower() in ingredient_names 
                                 for allergen in user_input.allergies)
                if has_allergen:
                    recipes_df = recipes_df[recipes_df['id'] != recipe.id]
        
        # 6. Filter by available ingredients (if provided)
        if user_input.inventory:
            inventory_lower = [item.lower() for item in user_input.inventory]
            # Prefer recipes with available ingredients
            recipes_df['has_ingredients'] = recipes_df['ingredients_text'].apply(
                lambda x: sum(1 for item in inventory_lower if item in x.lower())
            )
        else:
            recipes_df['has_ingredients'] = 0
        
        # 7. Filter by calorie target (±200 calories per meal)
        meal_calorie_target = calorie_target / 3  # Assuming 3 meals per day
        calorie_tolerance = 200
        
        # Only filter if we have recipes
        if not recipes_df.empty:
            filtered_df = recipes_df[
                (recipes_df['calories'] >= meal_calorie_target - calorie_tolerance) &
                (recipes_df['calories'] <= meal_calorie_target + calorie_tolerance)
            ]
            
            # If filtering removes all recipes, keep original
            if not filtered_df.empty:
                recipes_df = filtered_df
        
        # 8. Content-based filtering
        recipes_df = content_based_filtering(recipes_df, user_input.preferences, top_n=20)
        
        # 9. Collaborative filtering
        if user_input.user_id:
            recipes_df = collaborative_filtering(user_input.user_id, recipes_df, db, top_n=20)
        else:
            recipes_df['collab_score'] = 0.0
        
        # 10. Calculate final score (weighted combination)
        recipes_df['final_score'] = (
            0.4 * recipes_df['content_score'] +
            0.3 * recipes_df['collab_score'] +
            0.2 * (recipes_df['has_ingredients'] / max(recipes_df['has_ingredients'].max(), 1)) +
            0.1 * (1 - abs(recipes_df['calories'] - meal_calorie_target) / meal_calorie_target)
        )
        
        # 11. Sort and get top recommendations
        top_recipes = recipes_df.nlargest(5, 'final_score')
        
        # 12. Prepare response
        recommendations = top_recipes[['id', 'name', 'calories', 'protein', 'carbs', 'fat', 'prep_time']].to_dict(orient='records')
        
        explanation = f"Based on your {goal_type} goal, we recommend {len(recommendations)} meals. "
        explanation += f"Your daily calorie target is {calorie_target} kcal. "
        explanation += f"Target macros: {int(macros['protein'])}g protein, {int(macros['carbs'])}g carbs, {int(macros['fat'])}g fat."
        
        return RecommendationResponse(
            recommendations=recommendations,
            daily_calorie_target=calorie_target,
            macros=macros,
            explanation=explanation
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating recommendations: {str(e)}")

# ============================================
# Health Check
# ============================================

@app.get("/")
async def root():
    return {
        "message": "SmartMeal AI API v2.0",
        "status": "running",
        "endpoints": ["/recommend-meals/", "/health"]
    }

@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    """
    Check database connection and API health
    """
    try:
        # Test database connection
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        return {
            "status": "healthy",
            "database": "connected",
            "version": "2.0.0"
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": str(e)
        }

# ============================================
# WebSocket AI Assistant Endpoint
# ============================================

@app.websocket("/ws/assistant/{user_id}")
async def websocket_assistant_endpoint(websocket: WebSocket, user_id: str):
    """
    WebSocket endpoint for real-time AI cooking assistant
    """
    await manager.connect(websocket, user_id)
    try:
        while True:
            # Receive message from client
            data = await websocket.receive_text()
            message = json.loads(data)
            
            # Handle message
            await handle_websocket_message(websocket, user_id, message)
            
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
        print(f"User {user_id} disconnected from AI assistant")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("API_PORT", 8001))
    host = os.getenv("API_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, reload=True)