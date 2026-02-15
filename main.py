from __future__ import annotations
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import pandas as pd
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import text
import os
import json
from dotenv import load_dotenv

from database import get_db, Recipe, Meal  # User, Ingredient možeš dodati ako ti treba
from websocket_assistant import manager, handle_websocket_message
from nlp_hr import normalize_hr, strip_accents

load_dotenv()

app = FastAPI(title="SmartMeal AI API", version="2.1.0")

# =========================
# CORS
# =========================
# IMPORTANT: ako koristiš cookies/auth, stavi konkretne domene.
# Za demo može ovako, ali nemoj u produkciji ostaviti "*" + credentials=True.
ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Pydantic Models
# =========================
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
    diet_type: Optional[str] = None
    allergies: Optional[List[str]] = []


class RecommendationResponse(BaseModel):
    recommendations: List[Dict[str, Any]]
    daily_calorie_target: int
    macros: Dict[str, float]
    explanation: str


# =========================
# Calorie / Macro Helpers
# =========================
def calculate_bmr(age: int, gender: str, weight: float, height: float) -> float:
    if gender.lower() == "male":
        return (10 * weight) + (6.25 * height) - (5 * age) + 5
    return (10 * weight) + (6.25 * height) - (5 * age) - 161


def calculate_tdee(bmr: float, activity_level: str) -> float:
    activity_multipliers = {
        "sedentary": 1.2,
        "light": 1.375,
        "moderate": 1.55,
        "active": 1.725,
        "very_active": 1.9
    }
    mult = activity_multipliers.get(activity_level.lower(), 1.2)
    return bmr * mult


def calculate_calorie_target(
    age: int,
    gender: str,
    weight: float,
    height: float,
    activity_level: str,
    goal_type: str
) -> int:
    bmr = calculate_bmr(age, gender, weight, height)
    tdee = calculate_tdee(bmr, activity_level)

    if goal_type == "weight_loss":
        target = tdee - 500
    elif goal_type == "muscle_gain":
        target = tdee + 300
    else:
        target = tdee

    return max(1200, int(target))  # safety floor


def calculate_macros(calorie_target: int, goal_type: str) -> Dict[str, float]:
    if goal_type == "weight_loss":
        protein_ratio, carbs_ratio, fat_ratio = 0.35, 0.40, 0.25
    elif goal_type == "muscle_gain":
        protein_ratio, carbs_ratio, fat_ratio = 0.30, 0.45, 0.25
    else:
        protein_ratio, carbs_ratio, fat_ratio = 0.25, 0.45, 0.30

    return {
        "protein": (calorie_target * protein_ratio) / 4,
        "carbs": (calorie_target * carbs_ratio) / 4,
        "fat": (calorie_target * fat_ratio) / 9
    }


# =========================
# Content-based filtering (HR-friendly)
# =========================
_TFIDF_CACHE: Dict[str, Any] = {
    "vectorizer": None,
    "recipe_matrix": None,
    "recipe_ids": None,
    "descriptions": None,
}


def _build_description(name: str, ingredients_text: str) -> str:
    # HR normalizacija: ukloni dijakritiku za robusnost + lower
    n = strip_accents(normalize_hr(name))
    i = strip_accents(normalize_hr(ingredients_text))
    return f"{n} {i}".strip()


def content_based_filtering(
    recipes_df: pd.DataFrame,
    user_preferences: str,
    top_n: int = 10
) -> pd.DataFrame:
    if recipes_df.empty:
        return recipes_df

    # osiguraj column
    if "ingredients_text" not in recipes_df.columns:
        recipes_df["ingredients_text"] = ""

    # build descriptions
    recipes_df["description"] = recipes_df.apply(
        lambda r: _build_description(
            str(r.get("name", "")),
            str(r.get("ingredients_text", ""))
        ),
        axis=1
    )

    # vectorizer: bez english stopwords (HR)
    vectorizer = TfidfVectorizer(max_features=500)

    try:
        recipe_vectors = vectorizer.fit_transform(recipes_df["description"])
        user_vec = vectorizer.transform([_build_description(user_preferences, "")])
        sims = cosine_similarity(user_vec, recipe_vectors).flatten()
        recipes_df["content_score"] = sims
    except Exception:
        recipes_df["content_score"] = 0.0

    return recipes_df.nlargest(top_n, "content_score")


# =========================
# Collaborative filtering
# =========================
def collaborative_filtering(user_id: int, recipes_df: pd.DataFrame, db: Session) -> pd.DataFrame:
    if recipes_df.empty or not user_id:
        recipes_df["collab_score"] = 0.0
        return recipes_df

    user_meals = db.query(Meal).filter(Meal.user_id == user_id).all()
    user_recipe_ids = {m.recipe_id for m in user_meals}

    if not user_recipe_ids:
        recipes_df["collab_score"] = 0.0
        return recipes_df

    similar_users_meals = db.query(Meal).filter(
        Meal.recipe_id.in_(user_recipe_ids),
        Meal.user_id != user_id
    ).all()

    recipe_counts: Dict[int, int] = {}
    for meal in similar_users_meals:
        if meal.recipe_id not in user_recipe_ids:
            recipe_counts[meal.recipe_id] = recipe_counts.get(meal.recipe_id, 0) + 1

    recipes_df["collab_score"] = recipes_df["id"].map(lambda x: recipe_counts.get(int(x), 0))

    mx = float(recipes_df["collab_score"].max() or 0.0)
    if mx > 0:
        recipes_df["collab_score"] = recipes_df["collab_score"] / mx
    else:
        recipes_df["collab_score"] = 0.0

    return recipes_df


# =========================
# Main Recommendation Endpoint
# =========================
@app.post("/recommend-meals/", response_model=RecommendationResponse)
async def recommend_meals(user_input: UserInput, db: Session = Depends(get_db)):
    try:
        goal_type = (user_input.goals or {}).get("type", "maintenance")
        calorie_target = calculate_calorie_target(
            user_input.age,
            user_input.gender,
            user_input.weight,
            user_input.height,
            user_input.activity_level,
            goal_type
        )
        macros = calculate_macros(calorie_target, goal_type)

        # eager load ingredients to avoid N+1 (requires Recipe.ingredients relationship)
        recipes_query = db.query(Recipe).options(selectinload(Recipe.ingredients)).all()

        if not recipes_query:
            raise HTTPException(status_code=404, detail="No recipes found in database")

        recipes_data = []
        for recipe in recipes_query:
            ingredients_text = " ".join([getattr(ing, "name", "") for ing in getattr(recipe, "ingredients", [])])
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

        if recipes_df.empty:
            raise HTTPException(status_code=404, detail="No recipes available after processing")

        # allergies: substring match over ingredients list
        if user_input.allergies:
            allergies_norm = [strip_accents(normalize_hr(a)) for a in user_input.allergies if a]
            keep_ids = []

            for recipe in recipes_query:
                ing_names = [strip_accents(normalize_hr(getattr(ing, "name", ""))) for ing in getattr(recipe, "ingredients", [])]
                joined = " ".join(ing_names)
                has_allergen = any(a in joined for a in allergies_norm)

                if not has_allergen:
                    keep_ids.append(recipe.id)

            recipes_df = recipes_df[recipes_df["id"].isin(keep_ids)]

        # inventory preference score
        if user_input.inventory:
            inv = [strip_accents(normalize_hr(x)) for x in user_input.inventory if x]

            def inv_score(txt: str) -> int:
                t = strip_accents(normalize_hr(txt))
                return sum(1 for item in inv if item and item in t)

            recipes_df["has_ingredients"] = recipes_df["ingredients_text"].apply(inv_score)
        else:
            recipes_df["has_ingredients"] = 0

        # calorie filtering per meal
        meal_calorie_target = calorie_target / 3
        tolerance = 200

        filtered_df = recipes_df[
            (recipes_df["calories"] >= meal_calorie_target - tolerance) &
            (recipes_df["calories"] <= meal_calorie_target + tolerance)
        ]

        if not filtered_df.empty:
            recipes_df = filtered_df

        # content-based
        recipes_df = content_based_filtering(recipes_df, user_input.preferences, top_n=50)

        # collaborative
        if user_input.user_id:
            recipes_df = collaborative_filtering(user_input.user_id, recipes_df, db)
        else:
            recipes_df["collab_score"] = 0.0

        # final score (with safe clamp)
        max_has = float(recipes_df["has_ingredients"].max() or 0.0)
        if max_has <= 0:
            max_has = 1.0

        def calorie_closeness(cal: float) -> float:
            if meal_calorie_target <= 0:
                return 0.0
            score = 1.0 - abs(float(cal) - float(meal_calorie_target)) / float(meal_calorie_target)
            return max(0.0, min(1.0, score))

        recipes_df["cal_score"] = recipes_df["calories"].apply(calorie_closeness)

        recipes_df["final_score"] = (
            0.45 * recipes_df.get("content_score", 0.0) +
            0.30 * recipes_df.get("collab_score", 0.0) +
            0.15 * (recipes_df["has_ingredients"] / max_has) +
            0.10 * recipes_df["cal_score"]
        )

        top_recipes = recipes_df.nlargest(5, "final_score")
        recommendations = top_recipes[["id", "name", "calories", "protein", "carbs", "fat", "prep_time"]].to_dict(orient="records")

        explanation = (
            f"Na temelju cilja '{goal_type}' preporučujem {len(recommendations)} obroka. "
            f"Dnevni cilj: {calorie_target} kcal. "
            f"Makroi: ~{int(macros['protein'])}g proteina, ~{int(macros['carbs'])}g UH, ~{int(macros['fat'])}g masti."
        )

        return RecommendationResponse(
            recommendations=recommendations,
            daily_calorie_target=calorie_target,
            macros=macros,
            explanation=explanation
        )

    except HTTPException:
        # pusti FastAPI da vrati pravi status (npr. 404)
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating recommendations: {str(e)}")


# =========================
# Health / root
# =========================
@app.get("/")
async def root():
    return {
        "message": "SmartMeal AI API v2.1",
        "status": "running",
        "endpoints": ["/recommend-meals/", "/health", "/ws/assistant/{user_id}"]
    }


@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "connected", "version": "2.1.0"}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}


# =========================
# WebSocket endpoint
# =========================
@app.websocket("/ws/assistant/{user_id}")
async def websocket_assistant_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(websocket, user_id)

    try:
        while True:
            data = await websocket.receive_text()

            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "response",
                    "content": "Neispravan format poruke (mora biti JSON)."
                }))
                continue

            try:
                await handle_websocket_message(websocket, user_id, message)
            except Exception as e:
                print(f"AI assistant error for {user_id}: {e}")
                await websocket.send_text(json.dumps({
                    "type": "response",
                    "content": "Ups, došlo je do greške. Pokušaj ponovno."
                }))

    except WebSocketDisconnect:
        manager.disconnect(user_id)
        print(f"User {user_id} disconnected from AI assistant")

    except Exception as e:
        manager.disconnect(user_id)
        print(f"WebSocket fatal error for {user_id}: {e}")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("API_PORT", 8001))
    host = os.getenv("API_HOST", "127.0.0.1")

    uvicorn.run(app, host=host, port=port, reload=True)
