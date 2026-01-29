"""
WebSocket AI Chat Assistant for SmartMeal
Real-time cooking assistance, ingredient substitutions, and personalized advice
"""
from fastapi import WebSocket, WebSocketDisconnect
from typing import List, Dict, Optional
import json
import os
from dotenv import load_dotenv

load_dotenv()

# OpenAI API key (optional - can work without it using rule-based system)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.user_contexts: Dict[str, Dict] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.user_contexts[user_id] = {
            "current_recipe": None,
            "step": 0,
            "conversation_history": []
        }

    def disconnect(self, websocket: WebSocket, user_id: str):
        self.active_connections.remove(websocket)
        if user_id in self.user_contexts:
            del self.user_contexts[user_id]

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)

manager = ConnectionManager()

# ============================================
# AI Assistant Functions
# ============================================

def get_ingredient_substitutions(ingredient: str) -> List[Dict[str, str]]:
    """
    Suggest ingredient substitutions
    """
    substitutions = {
        "milk": [
            {"substitute": "bademovo mlijeko", "ratio": "1:1", "note": "Manje kalorija, bez laktoze"},
            {"substitute": "zobeno mlijeko", "ratio": "1:1", "note": "Kremasto, bez laktoze"},
            {"substitute": "kokosovo mlijeko", "ratio": "1:1", "note": "Bogatiji okus, više masti"}
        ],
        "butter": [
            {"substitute": "maslinovo ulje", "ratio": "3/4 šalice ulja na 1 šalicu maslaca", "note": "Zdravije masti"},
            {"substitute": "pire od jabuke", "ratio": "1:1", "note": "Manje kalorija, za pečenje"},
            {"substitute": "grčki jogurt", "ratio": "1:1", "note": "Više proteina, manje masti"}
        ],
        "sugar": [
            {"substitute": "med", "ratio": "3/4 šalice na 1 šalicu šećera", "note": "Prirodni zaslađivač"},
            {"substitute": "stevija", "ratio": "1 žličica na 1 šalicu šećera", "note": "Nula kalorija"},
            {"substitute": "javorov sirup", "ratio": "3/4 šalice na 1 šalicu šećera", "note": "Prirodan, bogat okus"}
        ],
        "egg": [
            {"substitute": "laneno jaje", "ratio": "1 žlica mljevenog lana + 3 žlice vode po jajetu", "note": "Veganski, puno vlakana"},
            {"substitute": "banana", "ratio": "1/4 zgnječene banane po jajetu", "note": "Dodaje slatkoću"},
            {"substitute": "pire od jabuke", "ratio": "1/4 šalice po jajetu", "note": "Niske kalorije"}
        ],
        "flour": [
            {"substitute": "bademovo brašno", "ratio": "1:1", "note": "Bez glutena, puno proteina"},
            {"substitute": "kokosovo brašno", "ratio": "1/4 šalice na 1 šalicu brašna", "note": "Bez glutena, upija tekućinu"},
            {"substitute": "zobeno brašno", "ratio": "1:1", "note": "Bez glutena, blag okus"}
        ]
    }
    
    ingredient_lower = ingredient.lower()
    for key in substitutions:
        # Prijevod ključnih riječi za pretragu
        translations = {
            "mlijeko": "milk", "maslac": "butter", "šećer": "sugar", "jaje": "egg", "brašno": "flour"
        }
        search_key = translations.get(key, key) # Ako korisnik upiše "mlijeko", tražimo "milk"
        
        if search_key in substitutions: # Ovo bi trebalo biti pametnije, ali za sad
             # Zapravo, logika gore je malo kriva, trebam provjeriti input
             pass

    # Jednostavnija logika s prijevodom inputa
    translations_map = {
        "mlijeko": "milk", "milk": "milk",
        "maslac": "butter", "butter": "butter",
        "šećer": "sugar", "sugar": "sugar",
        "jaje": "egg", "egg": "egg",
        "jaja": "egg",
        "brašno": "flour", "flour": "flour"
    }
    
    search_term = translations_map.get(ingredient_lower, ingredient_lower)
    
    for key in substitutions:
        if key in search_term:
            return substitutions[key]
    
    return [{"substitute": "Nisam pronašao zamjene", "ratio": "", "note": "Pokušaj s drugim sastojkom (npr. mlijeko, maslac, šećer, brašno)"}]

from sqlalchemy.orm import Session
from database import SessionLocal, Recipe, Ingredient
import random

# ... (ConnectionManager ostaje isti) ...

# ============================================
# Database Helpers
# ============================================

def get_db_session():
    return SessionLocal()

def find_recipes_in_db(query: str, db: Session, limit: int = 3, max_calories: int = None, meal_type: str = None) -> List[Dict]:
    """
    Search database for recipes matching the query with extra filters
    """
    query_lower = query.lower()
    base_query = db.query(Recipe)
    
    # 1. Filter by calories
    if max_calories:
        base_query = base_query.filter(Recipe.calories <= max_calories)
    
    # 2. Filter by meal type (heuristic keyword matching in name)
    if meal_type:
        # Npr. za "doručak" traži "kaša", "jaja", "omlet", "smoothie"
        if meal_type == "doručak":
             base_query = base_query.filter(
                 (Recipe.name.like("%kaša%")) | 
                 (Recipe.name.like("%jaja%")) | 
                 (Recipe.name.like("%omlet%")) | 
                 (Recipe.name.like("%smoothie%"))
             )
        elif meal_type == "večera":
             # Za večeru traži salate, losos, lagano
             base_query = base_query.filter(
                 (Recipe.name.like("%salata%")) | 
                 (Recipe.name.like("%riba%")) | 
                 (Recipe.name.like("%losos%")) | 
                 (Recipe.name.like("%tuna%"))
             )
    
    # 3. Text Search (if specified)
    if query:
        # Traži po imenu recepta
        results = base_query.filter(Recipe.name.like(f"%{query}%")).limit(limit).all()
        
        # Ako nema dovoljno i query nije prazan, probaj po sastojcima
        if len(results) < limit:
             ingredients = db.query(Ingredient).filter(Ingredient.name.like(f"%{query}%")).all()
             ing_ids = [ing.id for ing in ingredients]
             if ing_ids:
                 additional = db.query(Recipe).filter(Recipe.ingredients.any(Ingredient.id.in_(ing_ids))).limit(limit).all()
                 # Merge results (deduplicate)
                 existing_ids = {r.id for r in results}
                 for r in additional:
                     if r.id not in existing_ids:
                         results.append(r)
    else:
        # If no text query but have filters (e.g. "something light"), return random matching
        import random
        all_matching = base_query.all()
        if all_matching:
            results = random.sample(all_matching, min(limit, len(all_matching)))
        else:
            results = []

    final_list = []
    for r in results[:limit]:
        ing_list = ", ".join([ing.name for ing in r.ingredients])
        final_list.append({
            "name": r.name,
            "calories": r.calories,
            "ingredients": ing_list
        })
    
    return final_list

# ... (get_random_recipe remains same) ...

def get_cooking_tip(query: str, db: Session = None) -> str:
    """
    Provide cooking tips based on query with improved matching AND database search
    """
    query_lower = query.lower()
    
    # 1. Basic Conversation
    greetings = ["bok", "pozdrav", "zdravo", "hej", "dobar dan", "dobro jutro", "dobra večer"]
    if any(greet in query_lower for greet in greetings):
        return "Pozdrav! Ja sam tvoj AI kuhinjski asistent. Kako ti mogu pomoći danas? Mogu ti pronaći recept za doručak, večeru ili nešto lagano."
        
    thanks = ["hvala", "super", "odlično", "bravo"]
    if any(thk in query_lower for thk in thanks):
        return "Nema na čemu! Uživaj u kuhanju. 👨‍🍳"

    # 2. Recipe Search Intent & Filters
    # Detect filters from natural language
    max_cals = None
    target_meal = None
    search_term = ""
    
    # "Lagano" / "Mršavljenje" -> Low Calorie
    if "lagan" in query_lower or "mršav" in query_lower or "dijet" in query_lower or "malokalori" in query_lower:
        max_cals = 450
    
    # Meal types
    if "večer" in query_lower:
        target_meal = "večera"
    elif "doruč" in query_lower or "jutr" in query_lower:
        target_meal = "doručak"
        
    # Search keywords
    search_keywords = ["recept", "kuhah", "jesti", "večer", "ručak", "doruč", "gladan", "jela s", "sastoj", "nešto", "preporu"]
    should_search_db = any(kw in query_lower for kw in search_keywords) or max_cals or target_meal
    
    if should_search_db and db:
        # Extract search term (ingredient/dish name)
        stop_words = ["daj", "mi", "recept", "za", "s", "sa", "kako", "što", "da", "skuham", "danas", "neki", "neku", "nešto", "lagano", "super", "finu", "večeru", "doručak"]
        words = query_lower.split()
        potential_terms = [w for w in words if w not in stop_words and len(w) > 3]
        
        if potential_terms:
            search_term = potential_terms[0]
        
        # Call DB search with filters
        recipes = find_recipes_in_db(search_term, db, limit=3, max_calories=max_cals, meal_type=target_meal)
        
        if recipes:
            response = ""
            if max_cals:
                response += f"Evo nekoliko laganijih prijedloga (do {max_cals} kcal):\n"
            elif target_meal:
                response += f"Evo prijedloga za {target_meal}u:\n"
            else:
                response += f"Pronašao sam ovo za tebe:\n"
                
            for r in recipes:
                response += f"🔹 **{r['name']}** ({r['calories']} kcal)\n   *Sastojci: {r['ingredients']}*\n"
            return response
            
        elif max_cals and not recipes:
             return "Nisam pronašao ništa specifično, ali za lagani obrok preporučujem salatu s piletinom ili tunom. Pokušaj potražiti 'salata'."



    # 3. Cooking Tips Map (Keywords -> Response)
    tips_map = {
        ("kalorij", "mršav"): "Za smanjenje kalorija: koristi manje ulja (ili sprej za pečenje), peci u pećnici umjesto prženja, biraj nemasno meso i mliječne proizvode s manje masti, te jedi više povrća.",
        ("protein", "mišić", "masu"): "Za više proteina: dodaj piletinu, puretinu, jaja, svježi sir, grčki jogurt, tunu, leću ili grah u svoje obroke. Proteinski prah je također opcija za međuobrok.",
        ("vegan", "biljn"): "Za vegansku prehranu: zamijeni meso mahunarkama (grah, leća), tofuom ili tempehom. Umjesto mliječnih proizvoda koristi biljna mlijeka i jogurte. Lanene sjemenke su odlična zamjena za jaja u pečenju.",
        ("gluten", "brašn"): "Za bezglutensku prehranu: koristi rižu, kukuruz, krumpir, kvinoju ili heljdu. Umjesto pšeničnog brašna koristi gotove bezglutenske mješavine ili brašno od badema/kokosa.",
        ("brz", "vrijem", "žurb"): "Za brže kuhanje: koristi ekspres lonac, pripremaj namirnice unaprijed (meal prep), koristi smrznuto povrće (jednako je zdravo!) ili biraj one-pot recepte gdje se sve kuha u jednoj posudi.",
        ("okus", "začin", "fin"): "Za bolji okus bez puno kalorija: nemoj se bojati začina! Koristi češnjak, luk, svježe začinsko bilje (bosiljak, peršin), limunov sok, čili ili soja umak.",
    }
    
    for keys, tip in tips_map.items():
        if isinstance(keys, tuple):
            for key in keys:
                if key in query_lower:
                    return tip
        elif keys in query_lower:
            return tip
    
    # 4. Fallback logic for substitutions
    if "zamjen" in query_lower or "umjesto" in query_lower:
        return "Za zamjene sastojaka, najbolje je da napišeš samo ime sastojka (npr. 'mlijeko', 'jaja') ili koristiš opciju 'Substitute' ako je dostupna."

    return "Tu sam! Mogu ti naći recept (samo reci 'recept s piletinom'), dati savjet o kalorijama ili zamjenama. Što te zanima?"

# ... (ostatak koda za nutrition adjust) ...

async def handle_websocket_message(websocket: WebSocket, user_id: str, message: Dict):
    message_type = message.get("type", "chat")
    content = message.get("content", "")
    
    # Otvori DB sesiju za ovaj request
    db = get_db_session()
    
    try:
        if message_type == "chat":
            # Pass DB session to get_cooking_tip
            response = get_cooking_tip(content, db)
            await manager.send_personal_message(json.dumps({
                "type": "response",
                "content": response
            }), websocket)
        
        elif message_type == "substitute":
            # ... (ovo ostaje isto, ne treba DB osim ako ne želiš logirati)
            ingredient = message.get("ingredient", "")
            substitutions = get_ingredient_substitutions(ingredient)
            await manager.send_personal_message(json.dumps({
                "type": "substitutions",
                "ingredient": ingredient,
                "substitutions": substitutions
            }), websocket)

        # ... (ostali tipovi poruka) ...
        
    finally:
        db.close()

# Export manager and handler
__all__ = ["manager", "handle_websocket_message"]
