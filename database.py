"""
Database connection and models for SmartMeal AI
- Supports MySQL (default) and SQLite (optional)
- SQLAlchemy ORM models: User, Recipe, Ingredient, Meal
- Many-to-many: Recipe <-> Ingredient via ingredient_recipe (s pivot poljem quantity)
- Kompatibilnost sa starim nazivima *_per_100g preko izračunatih polja (bez zaokruživanja)
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    JSON,
    DateTime,
    ForeignKey,
    Table,
    Text,
    case,
    func,
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, column_property

load_dotenv()

# =========================
# Config
# =========================

DB_TYPE = os.getenv("DB_TYPE", "mysql").strip().lower()  # mysql | sqlite

# SQLite path (relative to this file) if DB_TYPE=sqlite
DB_PATH = os.getenv("DB_PATH", "../smartmeal/database/database.sqlite")

# MySQL settings (if DB_TYPE != sqlite)
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_DATABASE = os.getenv("DB_DATABASE", "smartmeal")
DB_USERNAME = os.getenv("DB_USERNAME", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

DEBUG_DB_URL = os.getenv("DEBUG_DB_URL", "0") == "1"

Base = declarative_base()

# =========================
# Database URL + Engine
# =========================

def _build_database_url() -> str:
    if DB_TYPE == "sqlite":
        import pathlib
        db_file = (pathlib.Path(__file__).parent / DB_PATH).resolve()
        return f"sqlite:///{db_file}"
    # MySQL (PyMySQL driver)
    return f"mysql+pymysql://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_DATABASE}"

DATABASE_URL = _build_database_url()

engine_kwargs = {}

if DB_TYPE == "sqlite":
    # SQLite threading fix for FastAPI
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # MySQL pooling (stabilnije u APIju)
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_recycle"] = int(os.getenv("DB_POOL_RECYCLE", "1800"))  # seconds
    engine_kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "5"))
    engine_kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "10"))

engine = create_engine(DATABASE_URL, echo=False, future=True, **engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

if DEBUG_DB_URL:
    # Nemoj u produkciji, jer može ispisati password
    print("DATABASE_URL =", DATABASE_URL)

# =========================
# Association table (many-to-many)
# =========================
# Laravel pivot: ingredient_recipe(ingredient_id, recipe_id, quantity, created_at, updated_at)
ingredient_recipe = Table(
    "ingredient_recipe",
    Base.metadata,
    Column("ingredient_id", Integer, ForeignKey("ingredients.id"), primary_key=True),
    Column("recipe_id", Integer, ForeignKey("recipes.id"), primary_key=True),
    Column("quantity", Float, nullable=True),
)

# =========================
# Models
# =========================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String(255), nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=True)

    goal = Column(String(255), nullable=True)

    # preferences as JSON: npr. {"likes":["tuna"], "dislikes":["gljive"]} ili string list
    preferences = Column(JSON, nullable=True)

    daily_calorie_target = Column(Integer, nullable=True)
    diet_type = Column(String(255), nullable=True)
    allergies = Column(JSON, nullable=True)
    available_ingredients = Column(JSON, nullable=True)

    age = Column(Integer, nullable=True)
    gender = Column(String(50), nullable=True)
    weight = Column(Float, nullable=True)
    height = Column(Float, nullable=True)
    activity_level = Column(String(50), nullable=True)

    meals = relationship("Meal", back_populates="user", cascade="all, delete-orphan")
    recipes = relationship("Recipe", back_populates="user", cascade="all, delete-orphan")


class Recipe(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True, index=True)

    # filtriranje po useru (tvoji recepti)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    name = Column(String(255), nullable=False)
    instructions = Column(Text, nullable=True)

    calories = Column(Integer, nullable=True)
    protein = Column(Integer, nullable=True)
    carbs = Column(Integer, nullable=True)
    fat = Column(Integer, nullable=True)
    prep_time = Column(Integer, nullable=True)

    user = relationship("User", back_populates="recipes")

    ingredients = relationship(
        "Ingredient",
        secondary=ingredient_recipe,
        back_populates="recipes",
        lazy="selectin",
    )

    meals = relationship("Meal", back_populates="recipe", cascade="all, delete-orphan")


class Ingredient(Base):
    __tablename__ = "ingredients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)

    # NOVA Laravel shema
    unit = Column(String(10), nullable=True)     # 'g' ili 'ml'
    ref_amount = Column(Integer, nullable=True)  # npr. 100

    calories = Column(Integer, nullable=True)
    protein = Column(Integer, nullable=True)
    carbs = Column(Integer, nullable=True)
    fat = Column(Integer, nullable=True)

    # Kompatibilnost: stari nazivi *_per_100g (bez zaokruživanja)
    calories_per_100g = column_property(
        case(
            ((ref_amount.isnot(None) & (ref_amount > 0)),
             (func.coalesce(calories, 0) * 100.0) / ref_amount),
            else_=0.0
        )
    )

    protein_per_100g = column_property(
        case(
            ((ref_amount.isnot(None) & (ref_amount > 0)),
             (func.coalesce(protein, 0) * 100.0) / ref_amount),
            else_=0.0
        )
    )

    carbs_per_100g = column_property(
        case(
            ((ref_amount.isnot(None) & (ref_amount > 0)),
             (func.coalesce(carbs, 0) * 100.0) / ref_amount),
            else_=0.0
        )
    )

    fat_per_100g = column_property(
        case(
            ((ref_amount.isnot(None) & (ref_amount > 0)),
             (func.coalesce(fat, 0) * 100.0) / ref_amount),
            else_=0.0
        )
    )

    recipes = relationship(
        "Recipe",
        secondary=ingredient_recipe,
        back_populates="ingredients",
        lazy="selectin",
    )


class Meal(Base):
    __tablename__ = "meals"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), index=True, nullable=False)

    date = Column(DateTime, nullable=True)
    meal_type = Column(String(50), nullable=True)

    user = relationship("User", back_populates="meals")
    recipe = relationship("Recipe", back_populates="meals")


# =========================
# FastAPI dependency
# =========================

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Optional helper (ako želiš ručno kreirati tablice)
def init_db():
    Base.metadata.create_all(bind=engine)