"""
Database connection and models for SmartMeal AI
"""
from sqlalchemy import create_engine, Column, Integer, String, Float, JSON, DateTime, ForeignKey, Table, Text  # ✅ DODANO Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import os
from dotenv import load_dotenv

load_dotenv()

# Database configuration
DB_TYPE = os.getenv("DB_TYPE", "mysql")  # MySQL je default
DB_PATH = os.getenv("DB_PATH", "../smartmeal/database/database.sqlite")

# Create database URL
if DB_TYPE == "sqlite":
    import pathlib
    db_file = pathlib.Path(__file__).parent / DB_PATH
    DATABASE_URL = f"sqlite:///{db_file}"
else:
    DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT = os.getenv("DB_PORT", "3306")
    DB_DATABASE = os.getenv("DB_DATABASE", "smartmeal")
    DB_USERNAME = os.getenv("DB_USERNAME", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DATABASE_URL = f"mysql+pymysql://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_DATABASE}"

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Association table for recipe-ingredient many-to-many relationship
ingredient_recipe = Table(
    'ingredient_recipe',
    Base.metadata,
    Column('ingredient_id', Integer, ForeignKey('ingredients.id')),
    Column('recipe_id', Integer, ForeignKey('recipes.id'))
)

# User model
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255))
    email = Column(String(255), unique=True, index=True)
    goal = Column(String(255))
    preferences = Column(JSON)
    daily_calorie_target = Column(Integer, nullable=True)
    diet_type = Column(String(255), nullable=True)
    allergies = Column(JSON, nullable=True)
    available_ingredients = Column(JSON, nullable=True)
    age = Column(Integer, nullable=True)
    gender = Column(String(50), nullable=True)
    weight = Column(Float, nullable=True)
    height = Column(Float, nullable=True)
    activity_level = Column(String(50), nullable=True)

    meals = relationship("Meal", back_populates="user")
    recipes = relationship("Recipe", back_populates="user")  # ✅ NOVO


# Recipe model
class Recipe(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True, index=True)

    # ✅ NOVO: user_id da možemo filtrirati “samo moje recepte”
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    name = Column(String(255))

    # ✅ NOVO: opis/upute recepta (mora postojati u MySQL tablici recipes)
    instructions = Column(Text, nullable=True)  # ✅ DODANO

    calories = Column(Integer)
    protein = Column(Integer)
    carbs = Column(Integer)
    fat = Column(Integer)
    prep_time = Column(Integer)

    # Relationships
    user = relationship("User", back_populates="recipes")  # ✅ NOVO
    ingredients = relationship("Ingredient", secondary=ingredient_recipe, back_populates="recipes")
    meals = relationship("Meal", back_populates="recipe")


# Ingredient model
class Ingredient(Base):
    __tablename__ = "ingredients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255))
    calories_per_100g = Column(Float, nullable=True)
    protein_per_100g = Column(Float, nullable=True)
    carbs_per_100g = Column(Float, nullable=True)
    fat_per_100g = Column(Float, nullable=True)

    recipes = relationship("Recipe", secondary=ingredient_recipe, back_populates="ingredients")


# Meal model
class Meal(Base):
    __tablename__ = "meals"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    recipe_id = Column(Integer, ForeignKey("recipes.id"))
    date = Column(DateTime)
    meal_type = Column(String(50))

    user = relationship("User", back_populates="meals")
    recipe = relationship("Recipe", back_populates="meals")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

print("DATABASE_URL =", DATABASE_URL)
