"""
WebSocket AI Chat Assistant for SmartMeal (Enhanced Version v2.1)

NEW Features v2.1:
✅ User goal integration (weight_loss, maintenance, muscle_gain)
✅ Allergies filtering (automatski isključi recepte s alergenima)
✅ Meal style detection: "nešto lagano", "brzo", "hranjivo", "zdravo"
✅ Recipe search (fuzzy) from user's DB
✅ Ingredient-based recipe search from user's DB (ALL + partial)
✅ Low-cal suggestions: "večera do 300 kcal" -> shows ONLY 3 short recommendations
✅ Selection flow: user picks 1/2/3 or name -> then cooking mode step-by-step
✅ Pagination: "novo" / "još" -> next 3 suggestions
✅ Substitutions: "zamjena za mlijeko", "nemam jaja"
✅ Cooking mode: "krenimo kuhati <recept>" + dalje/nazad/ponovi/stop
✅ Shopping list: "shopping lista za <recept>"
✅ Day meal plan: "plan obroka za danas" / "plan do 1800 kcal"
✅ Explanation: "zašto si ovo preporučio?"
✅ Nutrition: "koliko kalorija ima <recept/sastojak>", "koliko proteina ima <recept/sastojak>"
"""

from __future__ import annotations

from fastapi import WebSocket
from typing import List, Dict, Optional, Any, Tuple
import json
import re
import time
import datetime

from sqlalchemy.orm import Session
from sqlalchemy import or_, text

from database import SessionLocal, Recipe, Ingredient

from nlp_hr import normalize_hr, strip_accents, tokens_stemmed, stem_hr_token
from fuzzy_match import best_match


# =========================
# Connection Manager (multi-user safe)
# =========================

class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, WebSocket] = {}
        self.user_contexts: Dict[str, Dict[str, Any]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active[user_id] = websocket
        self.user_contexts.setdefault(user_id, {
            "current_recipe": None,
            "steps": [],
            "step": 0,
            "conversation_history": [],

            # ✅ selection/pagination context
            "last_suggestions": [],
            "suggestion_cursor": 0,
            "last_suggestion_mode": None,   # "low_cal" | "ingredients" | "plan" | "style"
            "last_meal_type": None,
            "last_max_cal": None,
            "last_style_filters": None,

            # ✅ plan context
            "last_plan": None,
            
            # ✅ 🆕 user profile context
            "user_goal": None,  # weight_loss, maintenance, muscle_gain
            "user_allergies": [],  # lista alergena
        })

    def disconnect(self, user_id: str):
        self.active.pop(user_id, None)
        self.user_contexts.pop(user_id, None)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    async def send_to_user(self, user_id: str, message: str):
        ws = self.active.get(user_id)
        if ws:
            await ws.send_text(message)


manager = ConnectionManager()


# =========================
# DB session helper
# =========================

def get_db_session() -> Session:
    return SessionLocal()


# =========================
# 🆕 USER PROFILE HELPERS
# =========================

def get_user_profile_from_db(user_id: int, db: Session) -> Dict[str, Any]:
    """
    Dohvati user goal i allergies iz users tablice
    """
    try:
        # Alternativno, raw SQL:
        sql = "SELECT goal, allergies FROM users WHERE id = :uid LIMIT 1"
        row = db.execute(text(sql), {"uid": user_id}).fetchone()
        
        if row:
            goal = row[0] if row[0] else None
            allergies_raw = row[1] if row[1] else None
            
            # 🆕 MySQL vraća JSON kao string, mora se parsirati
            if allergies_raw:
                if isinstance(allergies_raw, str):
                    try:
                        # Probaj parsirati kao JSON
                        allergies = json.loads(allergies_raw)
                        # Ako je string u stringu, parsiraj opet
                        if isinstance(allergies, str):
                            allergies = json.loads(allergies)
                    except:
                        # Ako nije JSON, možda je comma-separated string
                        allergies = [a.strip() for a in allergies_raw.split(',') if a.strip()]
                elif isinstance(allergies_raw, list):
                    allergies = allergies_raw
                else:
                    allergies = []
            else:
                allergies = []
            
            result = {
                "goal": goal,
                "allergies": allergies
            }
            
            # 🆕 Debug log
            print(f"📊 DB Query result for user {user_id}:")
            print(f"   Raw allergies: {repr(allergies_raw)} (type: {type(allergies_raw)})")
            print(f"   Parsed allergies: {allergies}")
            print(f"   Goal: {goal}")
            
            return result
        
        return {"goal": None, "allergies": []}
        
    except Exception as e:
        print(f"❌ Error fetching user profile: {e}")
        return {"goal": None, "allergies": []}


def load_user_profile(user_id: int, db: Session) -> None:
    """
    Load user profile u context pri connect-u ili na zahtjev
    """
    profile = get_user_profile_from_db(user_id, db)
    ctx = manager.user_contexts.get(str(user_id), {})
    ctx["user_goal"] = profile.get("goal")
    ctx["user_allergies"] = profile.get("allergies", [])
    manager.user_contexts[str(user_id)] = ctx
    
    # 🆕 Debug log
    print(f"✅ Loaded profile for user {user_id}: goal={profile.get('goal')}, allergies={profile.get('allergies', [])}")


def filter_recipes_by_allergies(recipes: List[Dict[str, Any]], allergies: List[str], db: Session) -> List[Dict[str, Any]]:
    """
    Filtriraj recepte koji sadrže alergene
    """
    if not allergies:
        return recipes
    
    # 🆕 Mapiraj engleski alergeni na hrvatske nazive
    allergen_mapping = {
        "eggs": ["jaja", "jaje", "egg"],
        "milk": ["mlijeko", "milk", "maslac", "sir", "jogurt", "vrhnje"],
        "gluten": ["brasno", "wheat", "psenica", "kruh", "tjestenina", "pasta"],
        "peanuts": ["kikiriki", "peanut"],
        "tree_nuts": ["orah", "badem", "ljesnjak", "pistachio", "cashew"],
        "soy": ["soja", "soy", "tofu"],
        "fish": ["riba", "tuna", "losos", "salmon"],
        "shellfish": ["skoljka", "shellfish", "lignja", "kozica", "shrimp"],
        "sesame": ["sezam", "sesame"]
    }
    
    # 🆕 Debug
    print(f"🔍 Filtering {len(recipes)} recipes for allergies: {allergies}")
    
    # Prošireni alergen lista (uključuje i eng i hr nazive)
    expanded_allergens = []
    for allergen in allergies:
        allergen_lower = allergen.lower()
        expanded_allergens.append(allergen_lower)
        
        # Dodaj sve mapove za taj alergen
        if allergen_lower in allergen_mapping:
            expanded_allergens.extend(allergen_mapping[allergen_lower])
    
    # Normaliziraj alergene
    allergies_norm = [strip_accents(normalize_hr(a)) for a in expanded_allergens if a]
    
    print(f"   Expanded allergens (normalized): {allergies_norm}")
    
    if not allergies_norm:
        return recipes
    
    safe_recipes = []
    
    for recipe in recipes:
        recipe_id = recipe.get("id")
        
        # Dohvati sastojke recepta
        ingredients_text = recipe.get("ingredients", "")
        
        if not ingredients_text:
            # Ako nema ingredients_text, dohvati iz baze
            try:
                sql = """
                    SELECT i.name
                    FROM ingredient_recipe ir
                    JOIN ingredients i ON i.id = ir.ingredient_id
                    WHERE ir.recipe_id = :rid
                """
                rows = db.execute(text(sql), {"rid": recipe_id}).fetchall()
                ingredients_list = [r[0] for r in rows if r and r[0]]
                ingredients_text = " ".join(ingredients_list)
            except:
                ingredients_text = ""
        
        # Normaliziraj sastojke
        ingredients_norm = strip_accents(normalize_hr(ingredients_text.lower()))
        
        # Provjeri da li sadrži alergene
        has_allergen = any(allergen in ingredients_norm for allergen in allergies_norm)
        
        if not has_allergen:
            safe_recipes.append(recipe)
            print(f"   ✅ SAFE: {recipe.get('name')} - ingredients: {ingredients_text[:50]}")
        else:
            # Debug log
            print(f"   ⚠️ FILTERED: {recipe.get('name')} - ingredients: {ingredients_text[:50]}")
    
    print(f"   Result: {len(safe_recipes)}/{len(recipes)} recipes are safe")
    
    return safe_recipes


def adjust_calories_for_goal(base_calories: int, goal: Optional[str]) -> Tuple[int, int]:
    """
    Prilagodi kalorijske limite prema cilju korisnika
    Vraća (min_cal, max_cal)
    """
    if not goal or goal == "maintenance":
        return (base_calories - 200, base_calories + 200)
    
    if goal == "weight_loss":
        # Preference za niže kalorije
        return (max(200, base_calories - 300), base_calories)
    
    if goal == "muscle_gain":
        # Preference za više kalorija i proteina
        return (base_calories, base_calories + 300)
    
    return (base_calories - 200, base_calories + 200)


def get_goal_friendly_message(goal: Optional[str]) -> str:
    """
    Generiraj user-friendly poruku o cilju
    """
    if goal == "weight_loss":
        return "🎯 Tvoj cilj: mršavljenje (preporučujem niže kalorije)"
    if goal == "muscle_gain":
        return "🎯 Tvoj cilj: jačanje mišića (preporučujem više proteina)"
    if goal == "maintenance":
        return "🎯 Tvoj cilj: održavanje težine"
    return ""


# =========================
# Regex / parsing helpers
# =========================

_RE_KEEP_HR = re.compile(r"[^\wčćđšž\s]+", re.IGNORECASE)

def _strip_punct_keep_hr(s: str) -> str:
    return _RE_KEEP_HR.sub(" ", (s or "")).strip()

def detect_meal_type(q: str) -> Optional[str]:
    t = normalize_hr(q)
    if "doruč" in t or "doruc" in t:
        return "doručak"
    if "ruč" in t or "ruc" in t:
        return "ručak"
    if "večer" in t or "vecer" in t:
        return "večera"
    return None

def meal_type_accusative(meal_type: str) -> str:
    return {
        "večera": "večeru",
        "doručak": "doručak",
        "ručak": "ručak",
    }.get(meal_type, meal_type)

def is_idea_question(q: str) -> bool:
    t = normalize_hr(q)
    triggers = ["što da jedem", "sta da jedem", "što skuhati", "sta skuhati", "ideja", "preporuči", "preporuci"]
    return any(x in t for x in triggers)

def extract_health_food(text_msg: str) -> str:
    t = normalize_hr(text_msg).replace("jel", "je li")
    m = re.search(r"je\s+li\s+(.+?)\s+zdrav", t)
    if not m:
        return ""
    food = m.group(1)
    food = re.sub(r"\b(ovo|to|ta|taj|hrana|jelo)\b", " ", food).strip()
    food = _strip_punct_keep_hr(food)
    food = re.sub(r"\s+", " ", food).strip()
    return food

def extract_max_calories(text_msg: str) -> Optional[int]:
    t = normalize_hr(text_msg)
    patterns = [
        r"\bdo\s+(\d{2,5})\s*(kcal|kalorija|kalorije)?\b",
        r"\bmax(?:imum)?\s+(\d{2,5})\s*(kcal|kalorija|kalorije)?\b",
        r"\bispod\s+(\d{2,5})\s*(kcal|kalorija|kalorije)?\b",
        r"\bmanje\s+od\s+(\d{2,5})\s*(kcal|kalorija|kalorije)?\b",
        r"\b<=\s*(\d{2,5})\s*(kcal|kalorija|kalorije)?\b",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            try:
                val = int(m.group(1))
                if 50 <= val <= 5000:
                    return val
            except Exception:
                pass
    return None

def extract_ingredient_from_text(text_msg: str) -> Optional[str]:
    t = normalize_hr(text_msg)
    patterns = [
        r"zamjen[a-zčćđšž]*\s+za\s+(.+)$",
        r"što\s+umjesto\s+(.+)$",
        r"sta\s+umjesto\s+(.+)$",
        r"umjesto\s+(.+)$",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            ing = m.group(1).strip(" .,!?:;\"'")
            ing = ing.split(" u ")[0].strip()
            ing = ing.split(" za ")[0].strip()
            return ing if ing else None
    return None

def extract_missing_ingredient(text_msg: str) -> Optional[str]:
    t = normalize_hr(text_msg)
    m = re.search(r"\b(ne\s*mam|nemam|nema)\s+(.+)$", t)
    if not m:
        return None
    ing = m.group(2).strip(" .,!?:;\"'")
    ing = ing.split(" ali ")[0].strip()
    ing = ing.split(" pa ")[0].strip()
    ing = ing.split(" nego ")[0].strip()
    return ing if ing else None

def extract_recipe_from_sentence(text_msg: str) -> str:
    t = normalize_hr(text_msg)
    if "recept" in t:
        t = t.split("recept", 1)[1].strip()
    t = re.sub(r"\b(za|mi|možeš|mozes|li|jel|je|imas|imaš|napisati|daj|molim|te|meni|da|bi)\b", " ", t)
    t = _strip_punct_keep_hr(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def extract_ingredients_after_imam(text_msg: str) -> str:
    t = normalize_hr(text_msg)
    if "imam" not in t:
        return ""
    part = t.split("imam", 1)[1]
    part = part.replace(":", " ")
    part = _strip_punct_keep_hr(part)
    part = re.sub(r"\b(i|te|pa|ali|samo|jos|još|imam)\b", " ", part)
    part = re.sub(r"\s+", " ", part).strip()
    return part

def extract_plan_target_kcal(text_msg: str) -> Optional[int]:
    t = normalize_hr(text_msg)
    m = re.search(r"\bplan\b.*?\b(\d{3,5})\s*(kcal|kalorija|kalorije)?\b", t)
    if m:
        try:
            v = int(m.group(1))
            if 800 <= v <= 5000:
                return v
        except Exception:
            return None
    return None

def wants_day_plan(q: str) -> bool:
    t = normalize_hr(q)
    return any(x in t for x in [
        "plan obroka", "plan za danas", "plan za sutra", "dnevni plan", "meal plan", "plan prehrane"
    ])


# =========================
# MEAL STYLE DETECTION
# =========================

def detect_meal_style(q: str) -> Dict[str, Any]:
    t = normalize_hr(q)
    filters: Dict[str, Any] = {}
    
    if any(x in t for x in ["brz", "hitno", "nemam vrem", "malo vrem", "kratko", "10 min", "20 min", "30 min"]):
        filters["max_prep_time"] = 30
        filters["style_tag"] = "brzo"
    
    if any(x in t for x in ["lagan", "lagano", "light", "osvjež", "osvježavajuć", "lake", "lako probavljiv"]):
        filters["max_calories"] = 400
        filters["style"] = "light"
        filters["style_tag"] = "lagano"
    
    if any(x in t for x in ["hranjiv", "sočn", "syt", "zasit", "zasitn", "masn"]):
        filters["min_protein"] = 20
        filters["style"] = "hearty"
        filters["style_tag"] = "hranjivo"
    
    if any(x in t for x in ["zdrav", "fit", "clean", "nutritvn", "balanced", "uravnotež"]):
        filters["healthy"] = True
        filters["max_calories"] = 500
        filters["style_tag"] = "zdravo"
    
    if any(x in t for x in ["tjestenin", "pasta", "spageti", "makaroni"]):
        filters["contains_ingredient"] = "tjestenina"
        filters["style_tag"] = "tjestenina"
    
    if any(x in t for x in ["salat"]):
        filters["contains_ingredient"] = "salata"
        filters["style_tag"] = "salata"
    
    if any(x in t for x in ["juha", "juh", "čorb", "corb", "supa"]):
        filters["recipe_type"] = "soup"
        filters["style_tag"] = "juha"
    
    if any(x in t for x in ["protein", "meso", "pilet", "riba", "tunj"]):
        filters["min_protein"] = 25
    
    return filters


def find_recipes_by_style(db: Session, user_id: int, style_filters: Dict[str, Any], limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
    query = db.query(Recipe).filter(Recipe.user_id == user_id)
    
    if "max_calories" in style_filters:
        query = query.filter(Recipe.calories <= style_filters["max_calories"])
    
    if "min_protein" in style_filters:
        if hasattr(Recipe, 'protein'):
            query = query.filter(Recipe.protein >= style_filters["min_protein"])
    
    if "max_prep_time" in style_filters:
        if hasattr(Recipe, 'prep_time'):
            query = query.filter(Recipe.prep_time <= style_filters["max_prep_time"])
    
    if "contains_ingredient" in style_filters:
        ingredient_name = style_filters["contains_ingredient"]
        query = (
            query.join(Recipe.ingredient_recipes)
            .join(Ingredient)
            .filter(Ingredient.name.like(f"%{ingredient_name}%"))
        )
    
    results = query.order_by(Recipe.name).offset(offset).limit(limit).all()
    
    out: List[Dict[str, Any]] = []
    for r in results:
        out.append({
            "id": r.id,
            "name": r.name,
            "calories": getattr(r, "calories", None),
            "prep_time": getattr(r, "prep_time", None),
            "ingredients": get_ingredients_for_recipe(r.id, db),
            "instructions": getattr(r, "instructions", None),
        })
    
    return out


def describe_style_filters(filters: Dict[str, Any]) -> str:
    parts = []
    
    if filters.get("style_tag") == "brzo":
        parts.append("brza jela (do 30 min)")
    if filters.get("style_tag") == "lagano":
        parts.append("lagana jela (do 400 kcal)")
    if filters.get("style_tag") == "hranjivo":
        parts.append("hranjiva jela (visoki proteini)")
    if filters.get("style_tag") == "zdravo":
        parts.append("zdrava jela (do 500 kcal)")
    if filters.get("style_tag") == "tjestenina":
        parts.append("jela s tjesteninom")
    if filters.get("style_tag") == "salata":
        parts.append("salate")
    if filters.get("style_tag") == "juha":
        parts.append("juhe i čorbe")
    
    if not parts:
        return "prijedloge"
    
    return " / ".join(parts)


# =========================
# Cooking mode: parse steps from instructions
# =========================

def parse_steps_from_instructions(instructions: str) -> List[str]:
    instr = (instructions or "").strip()
    if not instr:
        return []

    lines = [l.strip() for l in instr.splitlines() if l.strip()]
    numbered: List[str] = []
    for ln in lines:
        if re.match(r"^\s*(\d+[\.\)]|korak\s+\d+)\s*", ln.lower()):
            numbered.append(re.sub(r"^\s*(\d+[\.\)]|korak\s+\d+)\s*", "", ln, flags=re.IGNORECASE).strip())

    if len(numbered) >= 2:
        return numbered

    if len(lines) >= 2:
        return lines

    parts = re.split(r"\.\s+", instr)
    parts = [p.strip().strip(".") for p in parts if p.strip()]
    return parts


# =========================
# Substitutions
# =========================

def get_ingredient_substitutions(ingredient: str) -> List[Dict[str, str]]:
    substitutions = {
        "milk": [
            {"substitute": "bademovo mlijeko", "ratio": "1:1", "note": "Bez laktoze, blaži okus"},
            {"substitute": "zobeno mlijeko", "ratio": "1:1", "note": "Kremasto, super za kavu i kaše"},
            {"substitute": "sojino mlijeko", "ratio": "1:1", "note": "Najbliže po proteinima kravljem mlijeku"},
            {"substitute": "kravlje mlijeko bez laktoze", "ratio": "1:1", "note": "Ako ti smeta laktoza"},
        ],
        "butter": [
            {"substitute": "maslinovo ulje", "ratio": "3/4 šalice ulja na 1 šalicu maslaca", "note": "Zdravije masti"},
            {"substitute": "grčki jogurt", "ratio": "1:1", "note": "Super za pečenje"},
            {"substitute": "pire od jabuke", "ratio": "1:1", "note": "Manje kalorija (kolači)"},
        ],
        "sugar": [
            {"substitute": "med", "ratio": "3/4 šalice na 1 šalicu šećera", "note": "Prirodno"},
            {"substitute": "stevija", "ratio": "po uputama", "note": "Nula kalorija"},
            {"substitute": "javorov sirup", "ratio": "3/4 šalice", "note": "Bogat okus"},
        ],
        "egg": [
            {"substitute": "laneno jaje", "ratio": "1 žlica lana + 3 žlice vode", "note": "Veganski"},
            {"substitute": "banana", "ratio": "1/4 banane po jajetu", "note": "Palačinke/kolači"},
            {"substitute": "pire od jabuke", "ratio": "1/4 šalice po jajetu", "note": "Pečenje"},
        ],
        "flour": [
            {"substitute": "zobeno brašno", "ratio": "1:1", "note": "Blag okus"},
            {"substitute": "bademovo brašno", "ratio": "1:1", "note": "Bez glutena"},
            {"substitute": "kokosovo brašno", "ratio": "1/4 šalice", "note": "Upija tekućinu"},
        ],
    }

    translations_map = {
        "mlijek": "milk",
        "maslac": "butter",
        "secer": "sugar",
        "jaj": "egg",
        "brasn": "flour",
        "milk": "milk",
        "butter": "butter",
        "sugar": "sugar",
        "egg": "egg",
        "flour": "flour",
    }

    ing_key = stem_hr_token(strip_accents(normalize_hr(ingredient)))
    search_term = translations_map.get(ing_key, ing_key)

    if search_term in substitutions:
        return substitutions[search_term]

    keys = list(substitutions.keys())
    hit = best_match(search_term, keys, score_cutoff=75)
    if hit:
        key, _ = hit
        return substitutions[key]

    return [{
        "substitute": "Nisam pronašao zamjene",
        "ratio": "",
        "note": "Pokušaj s: mlijeko, maslac, šećer, jaje, brašno."
    }]


# =========================
# Ingredient list caching
# =========================

_ING_CACHE: Dict[int, Dict[str, object]] = {}
_ING_CACHE_TTL_SEC = 300

def _cache_get_user_ingredients(user_id: int) -> List[str]:
    now = time.time()
    hit = _ING_CACHE.get(user_id)
    if hit and (now - float(hit["ts"])) < _ING_CACHE_TTL_SEC:
        return hit["data"]  # type: ignore

    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT DISTINCT i.name
            FROM ingredients i
            JOIN ingredient_recipe ir ON ir.ingredient_id = i.id
            JOIN recipes r ON r.id = ir.recipe_id
            WHERE r.user_id = :uid
        """), {"uid": user_id}).fetchall()
        data = [r[0] for r in rows if r and r[0]]
        _ING_CACHE[user_id] = {"ts": now, "data": data}
        return data
    except Exception:
        try:
            rows = db.execute(text("SELECT name FROM ingredients")).fetchall()
            data = [r[0] for r in rows if r and r[0]]
            _ING_CACHE[user_id] = {"ts": now, "data": data}
            return data
        except Exception:
            _ING_CACHE[user_id] = {"ts": now, "data": []}
            return []
    finally:
        db.close()

def map_user_text_to_ingredient_names(user_id: int, raw_text: str) -> List[str]:
    choices = _cache_get_user_ingredients(user_id)
    if not choices:
        return []

    toks = tokens_stemmed(raw_text)
    mapped: List[str] = []
    for t in toks:
        hit = best_match(t, choices, score_cutoff=80)
        if hit:
            name, _ = hit
            mapped.append(name)

    seen = set()
    out: List[str] = []
    for x in mapped:
        k = normalize_hr(strip_accents(x))
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


# =========================
# Ingredients for recipe
# =========================

def get_ingredients_for_recipe(recipe_id: int, db: Session) -> str:
    sql = """
        SELECT i.name
        FROM ingredient_recipe ir
        JOIN ingredients i ON i.id = ir.ingredient_id
        WHERE ir.recipe_id = :rid
        ORDER BY i.name
    """
    try:
        rows = db.execute(text(sql), {"rid": recipe_id}).fetchall()
        names = [r[0] for r in rows] if rows else []
        return ", ".join(names).strip()
    except Exception:
        return ""


# =========================
# Output formatting
# =========================

def format_recipe_block(r: Dict[str, Any]) -> str:
    kcal = f" ({r['calories']} kcal)" if r.get('calories') is not None else ""
    out = f"🔹 {r.get('name','')}{kcal}\n"
    if r.get("ingredients"):
        out += f"   Sastojci: {r['ingredients']}\n"
    instr = (r.get("instructions") or "").strip()
    if instr:
        out += f"   Opis: {instr}\n"
    return out


def format_recipe_choice(r: Dict[str, Any], idx: int) -> str:
    kcal = f"{r.get('calories')} kcal" if r.get("calories") is not None else "? kcal"
    pt = r.get("prep_time")
    time_txt = f"{pt} min" if pt is not None else "?"
    ing = (r.get("ingredients") or "").strip()
    ing_list = [x.strip() for x in ing.split(",") if x.strip()]
    ing_preview = ", ".join(ing_list[:6])
    if len(ing_list) > 6:
        ing_preview += "…"
    if not ing_preview:
        ing_preview = "(nema sastojaka u bazi)"
    return f"{idx}) {r.get('name','')} — **{kcal}** — **{time_txt}**\n   Sastojci: {ing_preview}"

def store_suggestions(user_id: int, mode: str, cursor: int, items: List[Dict[str, Any]], style_filters: Optional[Dict[str, Any]] = None) -> None:
    ctx = manager.user_contexts.get(str(user_id), {})
    ctx["last_suggestions"] = items
    ctx["suggestion_cursor"] = cursor
    ctx["last_suggestion_mode"] = mode
    if style_filters:
        ctx["last_style_filters"] = style_filters
    manager.user_contexts[str(user_id)] = ctx

def present_suggestions(user_id: int, mode: str, cursor: int, items: List[Dict[str, Any]], style_filters: Optional[Dict[str, Any]] = None) -> str:
    if not items:
        return "Nemam više prijedloga za taj upit."

    store_suggestions(user_id, mode, cursor, items, style_filters)

    out = "Evo par prijedloga:\n\n"
    for i, r in enumerate(items, start=1):
        out += format_recipe_choice(r, i) + "\n\n"

    out += "Odaberi: `1`, `2`, `3` (ili napiši naziv). Za nove prijedloge napiši: `novo` / `još`."
    out += "\nZa cooking mode: nakon odabira možeš: `dalje`, `nazad`, `ponovi`, `stop`."
    return out

def parse_user_selection(text_msg: str) -> Optional[int]:
    q = normalize_hr(text_msg)

    m = re.search(r"\b([1-9])\b", q)
    if m:
        return int(m.group(1))

    if any(x in q for x in ["prvi", "prva"]):
        return 1
    if any(x in q for x in ["drugi", "druga"]):
        return 2
    if any(x in q for x in ["treci", "treći", "treca", "treća"]):
        return 3

    return None

def start_cooking_from_suggestion(user_id: int, suggestion: Dict[str, Any]) -> str:
    steps = parse_steps_from_instructions(suggestion.get("instructions") or "")
    if not steps:
        return f"Nažalost '{suggestion.get('name')}' nema korake u instructions."

    ukey = str(user_id)
    ctx = manager.user_contexts.get(ukey, {})
    ctx["current_recipe"] = suggestion
    ctx["steps"] = steps
    ctx["step"] = 0
    manager.user_contexts[ukey] = ctx

    return (
        f"🍳 Odabrano: {suggestion.get('name')}.\n"
        f"Korak 1/{len(steps)}: {steps[0]}\n"
        "Napiši: `dalje`, `ponovi`, `nazad`, `stop`."
    )


# =========================
# Recipe search (smarter)
# =========================

def find_recipes_in_db_smarter(query: str, db: Session, user_id: int, limit: int = 3) -> List[Dict[str, Any]]:
    q = normalize_hr(query)
    q = _strip_punct_keep_hr(q)
    q = re.sub(r"\s+", " ", q).strip()
    if not q:
        return []

    results = (
        db.query(Recipe)
        .filter(Recipe.user_id == user_id)
        .filter(Recipe.name.like(f"%{q}%"))
        .limit(limit)
        .all()
    )

    if not results:
        raw_toks = [t for t in q.split() if len(t) > 2]
        ors = [Recipe.name.like(f"%{t}%") for t in raw_toks[:8]]
        if ors:
            results = (
                db.query(Recipe)
                .filter(Recipe.user_id == user_id)
                .filter(or_(*ors))
                .limit(limit)
                .all()
            )

    if not results:
        rows = db.execute(text("SELECT name FROM recipes WHERE user_id=:uid"), {"uid": user_id}).fetchall()
        names = [r[0] for r in rows if r and r[0]]
        hit = best_match(q, names, score_cutoff=78)
        if hit:
            best_name, _ = hit
            results = (
                db.query(Recipe)
                .filter(Recipe.user_id == user_id)
                .filter(Recipe.name == best_name)
                .limit(limit)
                .all()
            )

    out: List[Dict[str, Any]] = []
    for r in results[:limit]:
        out.append({
            "id": r.id,
            "name": r.name,
            "calories": getattr(r, "calories", None),
            "prep_time": getattr(r, "prep_time", None),
            "ingredients": get_ingredients_for_recipe(r.id, db),
            "instructions": getattr(r, "instructions", None),
        })
    return out


# =========================
# Ingredient-based search
# =========================

def find_recipes_by_ingredient_names_all(ingredient_names: List[str], db: Session, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    if not ingredient_names:
        return []

    params: Dict[str, Any] = {"uid": user_id, "limit": limit}
    where_parts = []
    having_parts = []

    for idx, name in enumerate(ingredient_names[:8]):
        key = f"n{idx}"
        params[key] = name
        where_parts.append(f"i.name = :{key}")
        having_parts.append(f"SUM(CASE WHEN i.name = :{key} THEN 1 ELSE 0 END) > 0")

    sql = f"""
        SELECT r.id, r.name, r.calories, r.prep_time, r.instructions
        FROM recipes r
        JOIN ingredient_recipe ir ON ir.recipe_id = r.id
        JOIN ingredients i ON i.id = ir.ingredient_id
        WHERE r.user_id = :uid
          AND ({' OR '.join(where_parts)})
        GROUP BY r.id, r.name, r.calories, r.prep_time, r.instructions
        HAVING {' AND '.join(having_parts)}
        ORDER BY r.name
        LIMIT :limit
    """

    try:
        rows = db.execute(text(sql), params).fetchall()
        out: List[Dict[str, Any]] = []
        for rid, rname, rcal, rprep, rinstructions in rows:
            out.append({
                "id": int(rid),
                "name": str(rname),
                "calories": rcal,
                "prep_time": rprep,
                "ingredients": get_ingredients_for_recipe(int(rid), db),
                "instructions": rinstructions
            })
        return out
    except Exception:
        return []

def find_recipes_by_ingredient_names_partial(ingredient_names: List[str], db: Session, user_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    if not ingredient_names:
        return []

    params: Dict[str, Any] = {"uid": user_id, "limit": limit}
    where_parts = []
    score_parts = []

    for idx, name in enumerate(ingredient_names[:8]):
        key = f"n{idx}"
        params[key] = name
        where_parts.append(f"i.name = :{key}")
        score_parts.append(f"SUM(CASE WHEN i.name = :{key} THEN 1 ELSE 0 END)")

    sql = f"""
        SELECT r.id, r.name, r.calories, r.prep_time, r.instructions,
               ({' + '.join(score_parts)}) AS match_count
        FROM recipes r
        JOIN ingredient_recipe ir ON ir.recipe_id = r.id
        JOIN ingredients i ON i.id = ir.ingredient_id
        WHERE r.user_id = :uid
          AND ({' OR '.join(where_parts)})
        GROUP BY r.id, r.name, r.calories, r.prep_time, r.instructions
        ORDER BY match_count DESC, r.name ASC
        LIMIT :limit
    """

    try:
        rows = db.execute(text(sql), params).fetchall()
        out: List[Dict[str, Any]] = []
        for rid, rname, rcal, rprep, rinstructions, match_count in rows:
            out.append({
                "id": int(rid),
                "name": str(rname),
                "calories": rcal,
                "prep_time": rprep,
                "ingredients": get_ingredients_for_recipe(int(rid), db),
                "instructions": rinstructions,
                "match_count": int(match_count) if match_count is not None else 0
            })
        return out
    except Exception:
        return []

def find_recipes_under_calories(db: Session, user_id: int, max_calories: int, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
    if not max_calories or max_calories <= 0:
        return []
    results = (
        db.query(Recipe)
        .filter(Recipe.user_id == user_id)
        .filter(Recipe.calories <= max_calories)
        .order_by(Recipe.calories.asc(), Recipe.name.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for r in results:
        out.append({
            "id": r.id,
            "name": r.name,
            "calories": getattr(r, "calories", None),
            "prep_time": getattr(r, "prep_time", None),
            "ingredients": get_ingredients_for_recipe(r.id, db),
            "instructions": getattr(r, "instructions", None),
        })
    return out


# =========================
# Nutrition quick lookup
# =========================

def nutrition_lookup(text_msg: str, db: Session, user_id: int) -> Optional[str]:
    q = normalize_hr(text_msg)

    if not any(x in q for x in ["koliko", "kolko"]):
        return None
    if not any(x in q for x in ["kalor", "kcal", "protein"]):
        return None

    m = re.search(r"\bima\b\s+(.+)$", q)
    if not m:
        return None
    term = m.group(1).strip().replace("?", "").strip()

    recipes = find_recipes_in_db_smarter(term, db, user_id=user_id, limit=1)
    if recipes:
        r = recipes[0]
        if "protein" in q:
            rec = db.query(Recipe).filter(Recipe.id == r["id"]).first()
            if rec and getattr(rec, "protein", None) is not None:
                return f"🍽️ {r['name']}: oko {rec.protein} g proteina (po porciji prema bazi)."
            return f"🍽️ {r['name']}: nemam podatak o proteinima za ovaj recept."
        if r.get("calories") is not None:
            return f"🍽️ {r['name']}: {r['calories']} kcal (po porciji prema bazi)."
        return f"🍽️ {r['name']}: nemam upisane kalorije u bazi."

    rows = db.execute(text("SELECT name FROM ingredients")).fetchall()
    ing_names = [r[0] for r in rows if r and r[0]]
    hit = best_match(term, ing_names, score_cutoff=78)
    if not hit:
        return None

    best_ing, _ = hit
    ing = db.query(Ingredient).filter(Ingredient.name == best_ing).first()
    if not ing:
        return None

    if "protein" in q:
        if getattr(ing, "protein_per_100g", None) is not None:
            return f"🥗 {ing.name}: {ing.protein_per_100g} g proteina / 100g."
        return f"🥗 {ing.name}: nemam podatak o proteinima / 100g."

    if getattr(ing, "calories_per_100g", None) is not None:
        return f"🥗 {ing.name}: {ing.calories_per_100g} kcal / 100g."
    return f"🥗 {ing.name}: nemam podatak o kalorijama / 100g."


# =========================
# Day meal plan (from DB)
# =========================

def build_day_plan(db: Session, user_id: int, target_kcal: Optional[int] = None, user_goal: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    recipes = db.query(Recipe).filter(Recipe.user_id == user_id).all()
    if not recipes:
        return "Nema recepata u tvojoj bazi. Dodaj recepte pa mogu složiti plan.", {}

    # 🆕 Prilagodi target prema user goal-u
    if target_kcal is None:
        if user_goal == "weight_loss":
            target_kcal = 1600  # Defaultno niže za weight loss
        elif user_goal == "muscle_gain":
            target_kcal = 2400  # Defaultno više za muscle gain
        else:
            target_kcal = 1800  # Maintenance

    if target_kcal is not None:
        per_meal = max(200, target_kcal // 3)
    else:
        per_meal = 600

    scored: List[Tuple[float, Recipe]] = []
    for r in recipes:
        cal = getattr(r, "calories", None)
        if cal is None:
            continue
        scored.append((abs(float(cal) - float(per_meal)), r))

    if not scored:
        picks = recipes[:3]
    else:
        scored.sort(key=lambda x: x[0])
        picks = [x[1] for x in scored[:3]]

    slots = ["doručak", "ručak", "večera"]
    plan_items = []
    total_kcal = 0

    for slot, r in zip(slots, picks):
        cal = getattr(r, "calories", None) or 0
        total_kcal += int(cal)
        plan_items.append({
            "meal": slot,
            "id": r.id,
            "name": r.name,
            "calories": getattr(r, "calories", None),
            "prep_time": getattr(r, "prep_time", None),
            "ingredients": get_ingredients_for_recipe(r.id, db),
            "instructions": getattr(r, "instructions", None),
        })

    out = "📅 Plan obroka (iz tvoje baze):\n"
    
    # 🆕 Prikaži goal poruku
    if user_goal:
        out += get_goal_friendly_message(user_goal) + "\n\n"
    
    for it in plan_items:
        kcal = f"{it['calories']} kcal" if it.get("calories") is not None else "?"
        pt = f"{it['prep_time']} min" if it.get("prep_time") is not None else "?"
        out += f"\n✅ {it['meal'].capitalize()}: {it['name']} ({kcal}, {pt})\n"
        if it.get("ingredients"):
            out += f"   Sastojci: {it['ingredients']}\n"

    if target_kcal is not None:
        out += f"\nUkupno (odabrano): ~{total_kcal} kcal (cilj: {target_kcal} kcal)."
    else:
        out += f"\nUkupno (odabrano): ~{total_kcal} kcal."

    out += "\n\nMožeš pitati: 'zašto si ovo preporučio?' ili odaberi ručak: `1/2/3` pa idemo kuhati."
    ctx = {
        "target_kcal": target_kcal,
        "per_meal": per_meal,
        "total_kcal": total_kcal,
        "items": plan_items,
        "user_goal": user_goal
    }
    return out, ctx

def explain_last_plan(ctx: Dict[str, Any]) -> str:
    if not ctx or not ctx.get("items"):
        return "Nemam zadnje preporuke u kontekstu. Prvo zatraži 'plan obroka za danas' ili 'večera do 300 kcal'."

    mode = ctx.get("mode")
    user_goal = ctx.get("user_goal")
    
    lines = []
    
    # 🆕 Objasni alergije ako postoje
    user_allergies = ctx.get("user_allergies", [])
    if user_allergies:
        # Mapiraj engleski alergeni na hrvatske nazive za ljepši prikaz
        allergen_display = {
            "eggs": "jaja",
            "milk": "mliječne proizvode",
            "gluten": "gluten",
            "peanuts": "kikiriki",
            "tree_nuts": "orašaste plodove",
            "soy": "soju",
            "fish": "ribu",
            "shellfish": "školjke",
            "sesame": "sezam"
        }
        
        allergens_hr = []
        for allergen in user_allergies:
            allergen_lower = allergen.lower()
            allergens_hr.append(allergen_display.get(allergen_lower, allergen))
        
        if len(allergens_hr) == 1:
            lines.append(f"Alergičan si na {allergens_hr[0]} → automatski sam isključio sve recepte koji to sadrže.")
        else:
            lines.append(f"Alergičan si na: {', '.join(allergens_hr)} → automatski sam isključio sve recepte koji to sadrže.")
    
    # 🆕 Objasni goal ako postoji
    if user_goal:
        if user_goal == "weight_loss":
            lines.append("Tvoj cilj je mršavljenje → odabrao sam recepte s nižim kalorijama.")
        elif user_goal == "muscle_gain":
            lines.append("Tvoj cilj je jačanje mišića → odabrao sam recepte s više proteina i kalorija.")
        elif user_goal == "maintenance":
            lines.append("Tvoj cilj je održavanje → balansirao sam kalorije.")
    
    # Objašnjenje po modu
    if mode == "low_cal":
        meal_type = ctx.get("meal_type")
        max_cal = ctx.get("max_cal")
        lines.append(f"Tražio si {meal_type_accusative(meal_type)} do {max_cal} kcal.")
        lines.append("Odabrao sam recepte koji su najbliži tom limitu, sortirano od najnižih prema višim kalorijama.")
    
    elif mode == "style":
        desc = ctx.get("style_description")
        lines.append(f"Tražio si: {desc}.")
        lines.append("Filtrirao sam recepte prema tim kriterijima (brzina, lakoća, hranjivost itd.).")
    
    elif mode == "ingredients":
        lines.append("Pretraživao sam recepte koji sadrže sastojke koje imaš.")
        lines.append("Prioriziram recepte koji imaju sve navedene sastojke.")
    
    else:
        # Standardni plan obroka
        target = ctx.get("target_kcal")
        per_meal = ctx.get("per_meal")
        
        if target:
            lines.append(f"Odabrao sam recepte da budu blizu ~{per_meal} kcal po obroku (cilj {target} kcal/dan).")
        else:
            lines.append(f"Odabrao sam recepte koji su najbliže oko ~{per_meal} kcal po obroku (demo logika).")
        lines.append("Biranje je po najmanjoj razlici kalorija između recepta i ciljanog per-obrok unosa.")

    return "🧠 Zašto ove preporuke:\n" + "\n".join([f"• {l}" for l in lines])


# =========================
# Shopping list
# =========================

def build_shopping_list(db: Session, user_id: int, query: str) -> str:
    term = normalize_hr(query)
    term = term.replace("shopping lista", "").replace("kupovina", "").replace("kupov", "").replace("lista", "").strip()
    term = term.replace("za", " ").replace("recept", " ").strip()
    term = re.sub(r"\s+", " ", term).strip()

    recipes = find_recipes_in_db_smarter(term, db, user_id=user_id, limit=1)
    if not recipes:
        return "Napiši: 'shopping lista za <naziv recepta>'."

    r = recipes[0]
    ing = r.get("ingredients") or ""
    if not ing:
        return f"Našao sam '{r['name']}', ali nemam sastojke upisane u bazi."

    items = [x.strip() for x in ing.split(",") if x.strip()]
    out = f"🛒 Shopping lista za {r['name']}:\n"
    out += "\n".join([f"☐ {it}" for it in items])
    out += "\n\nAko želiš: 'krenimo kuhati " + r["name"] + "'."
    return out


# =========================
# Health answers
# =========================

def build_health_answer(food: str) -> str:
    f = normalize_hr(food)
    if not f:
        return "Možeš napisati: 'je li pizza zdrava' ili 'je li kajgana zdrava'."

    if "pizza" in f:
        return (
            "Pizza može biti OK povremeno 🙂\n"
            "Zdravija verzija: tanje tijesto, manje sira, više povrća i nemasni proteini."
        )
    if "kajgana" in f or "jaj" in strip_accents(f):
        return (
            "Kajgana je uglavnom dobra jer ima proteine.\n"
            "Bolje je s malo ulja/maslaca + uz salatu ili povrće."
        )

    return (
        f"{food.capitalize()} može biti zdravo ovisno o količini i načinu pripreme.\n"
        "Ako je prženo i masno → češće nije idealno; ako je pečeno/kuhano i uz povrće → bolja opcija."
    )


# =========================
# Cooking mode helpers
# =========================

def cooking_start(db: Session, user_id: int, raw_query: str) -> str:
    term = normalize_hr(raw_query)
    term = term.replace("krenimo kuhati", "").replace("kreni kuhati", "").replace("kreni kuhat", "").strip()
    term = term.replace("recept", "").strip()
    term = re.sub(r"\s+", " ", term).strip()

    recipes = find_recipes_in_db_smarter(term, db, user_id=user_id, limit=1)
    if not recipes:
        return "Ne nalazim taj recept u tvojoj bazi. Napiši: 'krenimo kuhati <naziv recepta>'."

    r = recipes[0]
    steps = parse_steps_from_instructions(r.get("instructions") or "")
    if not steps:
        return f"Našao sam '{r['name']}', ali nema koraka u uputama (instructions). Dodaj upute pa možemo u cooking mode."

    ukey = str(user_id)
    ctx = manager.user_contexts.get(ukey, {})
    ctx["current_recipe"] = r
    ctx["steps"] = steps
    ctx["step"] = 0
    manager.user_contexts[ukey] = ctx

    return (
        f"🍳 Krećemo s receptom: {r['name']}.\n"
        f"Korak 1/{len(steps)}: {steps[0]}\n"
        "Napiši: 'dalje', 'ponovi', 'nazad', 'stop'."
    )

def cooking_next(user_id: int) -> str:
    ctx = manager.user_contexts.get(str(user_id))
    if not ctx or not ctx.get("steps"):
        return "Nismo u cooking mode. Napiši: 'krenimo kuhati <naziv recepta>'."
    steps = ctx["steps"]
    idx = int(ctx.get("step", 0)) + 1
    if idx >= len(steps):
        ctx["step"] = len(steps) - 1
        return "✅ Gotovo! To je zadnji korak. Napiši 'ponovi' ili 'stop'."
    ctx["step"] = idx
    return f"Korak {idx+1}/{len(steps)}: {steps[idx]}"

def cooking_back(user_id: int) -> str:
    ctx = manager.user_contexts.get(str(user_id))
    if not ctx or not ctx.get("steps"):
        return "Nismo u cooking mode. Napiši: 'krenimo kuhati <naziv recepta>'."
    steps = ctx["steps"]
    idx = max(0, int(ctx.get("step", 0)) - 1)
    ctx["step"] = idx
    return f"Korak {idx+1}/{len(steps)}: {steps[idx]}"

def cooking_repeat(user_id: int) -> str:
    ctx = manager.user_contexts.get(str(user_id))
    if not ctx or not ctx.get("steps"):
        return "Nismo u cooking mode. Napiši: 'krenimo kuhati <naziv recepta>'."
    steps = ctx["steps"]
    idx = int(ctx.get("step", 0))
    return f"Korak {idx+1}/{len(steps)}: {steps[idx]}"

def cooking_stop(user_id: int) -> str:
    ukey = str(user_id)
    ctx = manager.user_contexts.get(ukey, {})
    ctx["current_recipe"] = None
    ctx["steps"] = []
    ctx["step"] = 0
    manager.user_contexts[ukey] = ctx
    return "🛑 Ok, izašli smo iz cooking mode."


# =========================
# Main chat logic
# =========================

def get_cooking_tip(query: str, db: Session, user_id: int) -> str:
    q = normalize_hr(query)
    
    # 🆕 Dohvati user context (goal i allergies)
    ctx = manager.user_contexts.get(str(user_id), {})
    user_goal = ctx.get("user_goal")
    user_allergies = ctx.get("user_allergies", [])

    # Quick nutrition lookup
    nutr = nutrition_lookup(query, db, user_id=user_id)
    if nutr:
        return nutr

    # help
    if any(x in q for x in ["što sve možeš", "sta sve mozes", "što možeš", "sta mozes", "kako mi možeš pomoći", "kako mi mozes pomoci"]):
        return (
            "Mogu ti pomoći ovako:\n"
            "• Recepti iz baze: 'recept pizza'\n"
            "• Pretraga po sastojcima: 'imam piletinu i rajcice'\n"
            "• Low-cal preporuke: 'večera do 300 kcal' (pa odaberi 1/2/3)\n"
            "• 🆕 Stil preporuke: 'nešto brzo', 'lagano jelo', 'hranjiva večera', 'zdravo'\n"
            "• Zamjene: 'zamjena za mlijeko' ili 'nemam jaja'\n"
            "• Cooking mode: 'krenimo kuhati <recept>' + dalje/nazad/ponovi/stop\n"
            "• Shopping lista: 'shopping lista za <recept>'\n"
            "• Plan dana: 'plan obroka za danas' ili 'plan do 1800 kcal'\n"
            "• Objašnjenje: 'zašto si ovo preporučio?'\n"
            "• Nutricija: 'koliko kalorija ima tuna' / 'koliko proteina ima piletina'\n"
            "• 🆕 Automatski izbjegavam recepte s tvojim alergenima!"
        )

    if any(x in q for x in ["bok", "pozdrav", "zdravo", "hej", "dobar dan", "dobro jutro", "dobra večer", "dobra vecer"]):
        greeting = "Tu sam! Probaj: 'nešto lagano za večeru', 'imam piletinu i rajcice', 'brzo jelo', ili 'večera do 300 kcal'."
        if user_goal:
            greeting += f"\n{get_goal_friendly_message(user_goal)}"
        return greeting

    if any(x in q for x in ["hvala", "super", "odlično", "odlicno", "bravo", "top"]):
        return "Nema na čemu! 😊"

    # Selection flow
    last_items: List[Dict[str, Any]] = ctx.get("last_suggestions") or []
    last_mode = ctx.get("last_suggestion_mode")
    cursor = int(ctx.get("suggestion_cursor", 0))

    # user wants more suggestions
    if any(x in q for x in ["novo", "jos", "još", "daj još", "daj jos", "drugo", "sljedece", "sljedeće"]):
        if last_mode == "low_cal":
            max_cal = ctx.get("last_max_cal")
            meal_type = ctx.get("last_meal_type")
            if max_cal and meal_type:
                new_cursor = cursor + 3
                items = find_recipes_under_calories(db, user_id=user_id, max_calories=int(max_cal), limit=3, offset=new_cursor)
                # 🆕 Filter allergies
                items = filter_recipes_by_allergies(items, user_allergies, db)
                header = f"Prijedlozi za {meal_type_accusative(str(meal_type))} do {int(max_cal)} kcal (nastavak):\n\n"
                return header + present_suggestions(user_id, "low_cal", new_cursor, items)
            return "Ok — napiši opet npr. `večera do 300 kcal` pa ću dati nove prijedloge."
        
        elif last_mode == "style":
            style_filters = ctx.get("last_style_filters")
            if style_filters:
                new_cursor = cursor + 3
                items = find_recipes_by_style(db, user_id=user_id, style_filters=style_filters, limit=3, offset=new_cursor)
                # 🆕 Filter allergies
                items = filter_recipes_by_allergies(items, user_allergies, db)
                desc = describe_style_filters(style_filters)
                header = f"Prijedlozi ({desc}) — nastavak:\n\n"
                return header + present_suggestions(user_id, "style", new_cursor, items, style_filters)
            return "Ok — napiši opet upit (npr. 'nešto lagano') pa ću dati nove prijedloge."
        
        return "Ok — napiši opet upit (npr. 'nešto brzo', 'večera do 300 kcal') pa ću dati nove prijedloge."

    # user selects 1/2/3
    sel = parse_user_selection(q)
    if sel and last_items and 1 <= sel <= len(last_items):
        chosen = last_items[sel - 1]
        return start_cooking_from_suggestion(user_id=user_id, suggestion=chosen)

    # selection by name from last suggestions
    if last_items and len(q.split()) <= 6 and last_mode in {"low_cal", "ingredients", "plan", "style"}:
        names = [x.get("name", "") for x in last_items if x.get("name")]
        hit = best_match(q, names, score_cutoff=80)
        if hit:
            name, _ = hit
            chosen = next((x for x in last_items if x.get("name") == name), None)
            if chosen:
                return start_cooking_from_suggestion(user_id=user_id, suggestion=chosen)

    # Explain last plan / recommendations
    if "zašto" in q and any(x in q for x in ["prepor", "ovo", "ovaj", "plan"]):
        last = ctx.get("last_plan")
        if last:
            return explain_last_plan(last)
        return "Nemam zadnje preporuke. Prvo zatraži 'plan obroka za danas' ili 'večera do 300 kcal'."

    # Day plan
    if wants_day_plan(q):
        target = extract_plan_target_kcal(q)
        txt, plan_ctx = build_day_plan(db, user_id=user_id, target_kcal=target, user_goal=user_goal)
        manager.user_contexts[str(user_id)]["last_plan"] = plan_ctx
        
        # 🆕 Filter allergies from plan
        plan_items = plan_ctx.get("items", [])
        plan_items = filter_recipes_by_allergies(plan_items, user_allergies, db)
        
        store_suggestions(user_id, mode="plan", cursor=0, items=plan_items)
        return txt

    # Shopping list
    if "shopping" in q or "kupov" in q or ("lista" in q and "za" in q):
        return build_shopping_list(db, user_id=user_id, query=q)

    # Cooking mode commands
    if any(x in q for x in ["krenimo kuhati", "kreni kuhati", "kreni kuhat", "kuhajmo"]):
        return cooking_start(db, user_id=user_id, raw_query=q)

    if q in ["dalje", "sljedece", "sljedeći", "sljedeci"]:
        return cooking_next(user_id)

    if q in ["nazad", "prethodni", "back"]:
        return cooking_back(user_id)

    if q in ["ponovi", "repeat"]:
        return cooking_repeat(user_id)

    if q in ["stop", "stani", "prekini"]:
        return cooking_stop(user_id)

    # Substitutions: "nemam X"
    missing = extract_missing_ingredient(q)
    if missing:
        subs = get_ingredient_substitutions(missing)
        lines = [f"• {s['substitute']} ({s.get('ratio','')}) — {s.get('note','')}" for s in subs]
        return f"Nemam '{missing}' — možeš ovako:\n" + "\n".join(lines)

    # Substitutions: "zamjena za X"
    ing = extract_ingredient_from_text(q)
    if ing:
        subs = get_ingredient_substitutions(ing)
        return "Zamjene:\n" + "\n".join([f"• {s['substitute']} ({s.get('ratio','')}) — {s.get('note','')}" for s in subs])

    # Health: "je li X zdravo"
    if ("zdrav" in q) and ("je li" in q or "jel" in q):
        food = extract_health_food(q)
        return build_health_answer(food)

    # MEAL STYLE DETECTION
    style_filters = detect_meal_style(q)
    if style_filters:
        ctx["suggestion_cursor"] = 0
        ctx["last_style_filters"] = style_filters

        items = find_recipes_by_style(db, user_id=user_id, style_filters=style_filters, limit=3, offset=0)
        
        # 🆕 Filter allergies
        items = filter_recipes_by_allergies(items, user_allergies, db)
        
        if items:
            desc = describe_style_filters(style_filters)
            
            # 🆕 Store last_plan context za "zašto si ovo preporučio?"
            plan_context = {
                "mode": "style",
                "style_filters": style_filters,
                "style_description": desc,
                "user_goal": user_goal,
                "user_allergies": user_allergies,  # 🆕
                "items": items,
                "explanation": f"Tražio si {desc}."
            }
            ctx["last_plan"] = plan_context
            manager.user_contexts[str(user_id)] = ctx
            
            header = f"Prijedlozi ({desc}) iz tvoje baze:\n\n"
            return header + present_suggestions(user_id, "style", 0, items, style_filters)
        
        desc = describe_style_filters(style_filters)
        return f"Nemam recepte koji odgovaraju: {desc}. Dodaj još recepata ili probaj drugačiji upit."

    # Low-cal suggestions per meal type
    meal_type = detect_meal_type(q)
    max_cal = extract_max_calories(q)
    if meal_type and max_cal is not None:
        # 🆕 Adjust calories based on goal
        if user_goal:
            min_cal, adjusted_max = adjust_calories_for_goal(max_cal, user_goal)
            max_cal = adjusted_max
        
        ctx["suggestion_cursor"] = 0
        ctx["last_meal_type"] = meal_type
        ctx["last_max_cal"] = int(max_cal)
        
        items = find_recipes_under_calories(db, user_id=user_id, max_calories=max_cal, limit=3, offset=0)
        
        # 🆕 Filter allergies
        items = filter_recipes_by_allergies(items, user_allergies, db)
        
        if items:
            # 🆕 Store last_plan context za "zašto si ovo preporučio?"
            plan_context = {
                "mode": "low_cal",
                "meal_type": meal_type,
                "max_cal": max_cal,
                "user_goal": user_goal,
                "user_allergies": user_allergies,  # 🆕
                "items": items,
                "explanation": f"Tražio si {meal_type_accusative(meal_type)} do {max_cal} kcal."
            }
            ctx["last_plan"] = plan_context
            manager.user_contexts[str(user_id)] = ctx
            
            header = f"Prijedlozi za {meal_type_accusative(meal_type)} do {max_cal} kcal (iz tvoje baze):\n\n"
            if user_goal:
                header += get_goal_friendly_message(user_goal) + "\n\n"
            return header + present_suggestions(user_id, "low_cal", 0, items)

        return f"Nemam recepte u tvojoj bazi do {max_cal} kcal. Dodaj još recepata ili povećaj limit."

    # "imam ..." -> recipe suggestions by ingredients
    if "imam" in q:
        raw = extract_ingredients_after_imam(q)
        if not raw:
            return "Napiši što imaš (npr. 'imam piletinu i jaja') pa ću predložiti recepte."

        ing_names = map_user_text_to_ingredient_names(user_id, raw)
        if not ing_names:
            return "Ne prepoznajem sastojke iz tvoje baze. Probaj jednostavnije nazive (npr. 'piletina, jaja, rajčica')."

        recipes_all = find_recipes_by_ingredient_names_all(ing_names, db, user_id=user_id, limit=10)
        
        # 🆕 Filter allergies
        recipes_all = filter_recipes_by_allergies(recipes_all, user_allergies, db)
        
        if recipes_all:
            first = recipes_all[:3]
            header = "Na temelju sastojaka koje imaš, evo par prijedloga:\n\n"
            return header + present_suggestions(user_id, "ingredients", 0, first)

        partial = find_recipes_by_ingredient_names_partial(ing_names, db, user_id=user_id, limit=10)
        
        # 🆕 Filter allergies
        partial = filter_recipes_by_allergies(partial, user_allergies, db)
        
        if partial:
            first = partial[:3]
            header = f"Nemam recept koji sadrži SVE: {', '.join(ing_names)}.\nAli imam najbliže prijedloge:\n\n"
            return header + present_suggestions(user_id, "ingredients", 0, first)

        return f"Nemam recepte koji odgovaraju sastojcima: {', '.join(ing_names)}."

    # "recept ..." -> show full details
    if "recept" in q:
        term = extract_recipe_from_sentence(q)
        recipes = find_recipes_in_db_smarter(term or q, db, user_id=user_id, limit=3)
        if recipes:
            out = "Evo što imam u bazi:\n"
            for r in recipes:
                out += format_recipe_block(r)
            return out
        return "Nemam taj recept u tvojoj bazi. Dodaj ga u svoje recepte ili napiši naziv nekog svog recepta."

    if is_idea_question(q):
        return (
            "Reci mi jednu od opcija 🙂\n"
            "• 'večera do 300 kcal'\n"
            "• 'nešto brzo / lagano / hranjivo / zdravo'\n"
            "• 'imam <sastojci>'\n"
            "• 'plan obroka za danas'\n"
            "• 'recept <naziv>'"
        )

    return "Tu sam! Probaj: 'nešto lagano za večeru', 'brzo jelo', 'večera do 300 kcal', 'imam piletinu i rajcice', ili 'krenimo kuhati pizza'."


# =========================
# WebSocket handler
# =========================

async def handle_websocket_message(websocket: WebSocket, user_id: str, message: Dict[str, Any]):
    message_type = message.get("type", "chat")
    content = message.get("content", "")

    db = get_db_session()
    try:
        try:
            uid = int(user_id)
        except Exception:
            uid = 0

        # 🆕 Load user profile ako već nije učitan
        ctx = manager.user_contexts.get(user_id, {})
        if ctx.get("user_goal") is None:
            load_user_profile(uid, db)

        if message_type == "chat":
            # substitutions direct -> structured response for UI
            ing = extract_ingredient_from_text(content)
            if ing:
                subs = get_ingredient_substitutions(ing)
                await manager.send_personal_message(json.dumps({
                    "type": "substitutions",
                    "ingredient": ing,
                    "substitutions": subs
                }), websocket)
                return

            # single word substitutions
            only_word = normalize_hr(content)
            if stem_hr_token(strip_accents(only_word)) in {"mlijek", "maslac", "secer", "jaj", "brasn"}:
                subs = get_ingredient_substitutions(only_word)
                await manager.send_personal_message(json.dumps({
                    "type": "substitutions",
                    "ingredient": only_word,
                    "substitutions": subs
                }), websocket)
                return

            response = get_cooking_tip(content, db, user_id=uid)
            await manager.send_personal_message(json.dumps({
                "type": "response",
                "content": response
            }), websocket)

        elif message_type == "substitute":
            ingredient = message.get("ingredient", "")
            subs = get_ingredient_substitutions(ingredient)
            await manager.send_personal_message(json.dumps({
                "type": "substitutions",
                "ingredient": ingredient,
                "substitutions": subs
            }), websocket)

        else:
            await manager.send_personal_message(json.dumps({
                "type": "response",
                "content": "Ne prepoznajem tip poruke."
            }), websocket)

    except Exception as e:
        await manager.send_personal_message(json.dumps({
            "type": "response",
            "content": f"Ups, došlo je do greške u asistentu: {str(e)}"
        }), websocket)

    finally:
        db.close()


__all__ = ["manager", "handle_websocket_message"]