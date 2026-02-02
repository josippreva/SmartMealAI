"""
WebSocket AI Chat Assistant for SmartMeal
Real-time cooking assistance, ingredient substitutions,
and recipe search + ingredients from MySQL (JOIN) - plus DEMO features.
"""

from fastapi import WebSocket
from typing import List, Dict, Optional, Tuple
import json
import re
from sqlalchemy.orm import Session
from sqlalchemy import or_, text

from database import SessionLocal, Recipe, Ingredient


# =========================
# Connection Manager
# =========================

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
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if user_id in self.user_contexts:
            del self.user_contexts[user_id]

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)


manager = ConnectionManager()


# =========================
# Helpers
# =========================

def get_db_session() -> Session:
    return SessionLocal()

def normalize_text(s: str) -> str:
    return (s or "").strip().lower()

def _strip_punct_keep_hr(s: str) -> str:
    return re.sub(r"[^\wčćđšž\s]+", " ", (s or ""), flags=re.IGNORECASE).strip()

def extract_ingredient_from_text(text_msg: str) -> Optional[str]:
    """
    Extract ingredient from:
    - "zamjena za mlijeko"
    - "što umjesto mlijeka"
    - "umjesto jaja"
    """
    t = normalize_text(text_msg)
    patterns = [
        r"zamjen[a-zčćđšž]*\s+za\s+(.+)$",
        r"što\s+umjesto\s+(.+)$",
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

def _clean_recipe_query(q: str) -> str:
    """
    Removes only leading forms:
    - "recept za pileća salata" -> "pileća salata"
    - "recept pileća salata" -> "pileća salata"
    """
    t = normalize_text(q)
    # ukloni sve prije riječi "recept" ako postoji
    if "recept" in t:
        t = t.split("recept", 1)[1].strip()

    # ukloni "za" na početku
    t = re.sub(r"^\s*za\s+", "", t).strip()
    t = t.strip(":").strip()
    return t

def extract_recipe_from_sentence(text_msg: str) -> str:
    """
    Works for:
    - "možeš li mi napisati recept kajgana"
    - "daj recept za pizzu"
    - "imaš li recept pileća salata"
    Returns: "kajgana", "pizzu", "pileća salata"
    """
    t = normalize_text(text_msg)

    if "recept" in t:
        t = t.split("recept", 1)[1].strip()

    # remove common filler words
    t = re.sub(r"\b(za|mi|možeš|mozes|li|jel|je|imas|imaš|napisati|daj|molim|te|meni|da|bi)\b", " ", t)
    t = _strip_punct_keep_hr(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _tokenize_for_search(q: str) -> List[str]:
    stop_words = {"za", "s", "sa", "u", "na", "od", "i", "ili", "mi", "meni", "te"}
    tokens = [t for t in re.split(r"\s+", normalize_text(q)) if t]
    tokens = [t for t in tokens if t not in stop_words and len(t) > 2]
    return tokens[:8]

def extract_ingredients_after_imam(text_msg: str) -> List[str]:
    """
    "imam piletinu i jaja" -> ["piletinu", "jaja"]
    "imam: tuna, salata" -> ["tuna", "salata"]
    """
    t = normalize_text(text_msg)
    if "imam" not in t:
        return []

    part = t.split("imam", 1)[1]
    part = part.replace(":", " ")
    part = _strip_punct_keep_hr(part)
    part = re.sub(r"\b(i|te|pa|ali|samo|jos|još)\b", " ", part)
    part = re.sub(r"\s+", " ", part).strip()

    if not part:
        return []

    # split by commas if user had them originally (already removed punct),
    # so just tokenize and keep "food-like" tokens
    toks = _tokenize_for_search(part)
    return toks

def extract_health_food(text_msg: str) -> str:
    """
    "je li pizza zdrava" -> "pizza"
    "jel kajgana zdrava" -> "kajgana"
    """
    t = normalize_text(text_msg)
    t = t.replace("jel", "je li")
    # grab after "je li" or "je li to"
    m = re.search(r"je\s+li\s+(.+?)\s+zdrav", t)
    if m:
        food = m.group(1)
        food = re.sub(r"\b(ovo|to|ta|taj|ta\s+hrana|jelo)\b", " ", food).strip()
        food = _strip_punct_keep_hr(food)
        food = re.sub(r"\s+", " ", food).strip()
        return food
    return ""


# =========================
# Substitutions (Rule-based)
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
        "mlijeko": "milk", "mlijeka": "milk", "milk": "milk",
        "maslac": "butter", "butter": "butter",
        "šećer": "sugar", "secer": "sugar", "sugar": "sugar",
        "jaje": "egg", "jaja": "egg", "egg": "egg",
        "brašno": "flour", "brasno": "flour", "flour": "flour",
    }

    ing = normalize_text(ingredient)
    search_term = translations_map.get(ing, ing)

    for key in substitutions.keys():
        if key == search_term or key in search_term:
            return substitutions[key]

    return [{
        "substitute": "Nisam pronašao zamjene",
        "ratio": "",
        "note": "Pokušaj s: mlijeko, maslac, šećer, jaje, brašno."
    }]


# =========================
# Ingredients from MySQL (JOIN) + fallback
# =========================

def get_ingredients_for_recipe(recipe_id: int, db: Session) -> str:
    """
    Reads ingredients with JOIN. If your pivot columns differ, fallback tries alternatives.
    """
    sql_variants = [
        """
        SELECT i.name
        FROM ingredient_recipe ir
        JOIN ingredients i ON i.id = ir.ingredient_id
        WHERE ir.recipe_id = :rid
        ORDER BY i.name
        """,
        """
        SELECT i.name
        FROM ingredient_recipe ir
        JOIN ingredients i ON i.id = ir.ingredient_id
        WHERE ir.recipes_id = :rid
        ORDER BY i.name
        """,
        """
        SELECT i.name
        FROM ingredient_recipe ir
        JOIN ingredients i ON i.id = ir.ingredients_id
        WHERE ir.recipe_id = :rid
        ORDER BY i.name
        """,
    ]

    for sql in sql_variants:
        try:
            rows = db.execute(text(sql), {"rid": recipe_id}).fetchall()
            names = [r[0] for r in rows] if rows else []
            txt = ", ".join(names).strip()
            if txt:
                return txt
        except Exception:
            continue

    return ""


# =========================
# Recipe Search (DB) by name + ingredients
# =========================

def find_recipes_in_db(query: str, db: Session, limit: int = 3) -> List[Dict]:
    """
    Search recipes by name (phrase / OR tokens).
    Then attaches ingredients via JOIN function.
    """
    q_raw = (query or "").strip()
    if not q_raw:
        return []

    q_clean = _clean_recipe_query(q_raw)
    tokens = _tokenize_for_search(q_clean)

    results = []

    if q_clean:
        results = db.query(Recipe).filter(Recipe.name.like(f"%{q_clean}%")).limit(limit).all()

    if not results and tokens:
        ors = [Recipe.name.like(f"%{t}%") for t in tokens]
        results = db.query(Recipe).filter(or_(*ors)).limit(limit).all()

    final = []
    for r in results[:limit]:
        ing_txt = get_ingredients_for_recipe(r.id, db)
        final.append({
            "id": r.id,
            "name": r.name,
            "calories": getattr(r, "calories", None),
            "ingredients": ing_txt
        })

    return final

def find_recipes_by_ingredient_tokens(tokens: List[str], db: Session, limit: int = 3) -> List[Dict]:
    """
    For messages like:
    - "imam piletinu i jaja"
    It finds recipes that have ingredients matching any token.
    """
    tokens = [t for t in (tokens or []) if t and len(t) > 2]
    if not tokens:
        return []

    # Build dynamic OR for ingredient name LIKE
    where_parts = []
    params = {"limit": limit}
    for idx, tok in enumerate(tokens[:8]):
        key = f"t{idx}"
        where_parts.append(f"i.name LIKE :{key}")
        params[key] = f"%{tok}%"

    where_sql = " OR ".join(where_parts)

    sql = f"""
        SELECT DISTINCT r.id, r.name, r.calories
        FROM recipes r
        JOIN ingredient_recipe ir ON ir.recipe_id = r.id
        JOIN ingredients i ON i.id = ir.ingredient_id
        WHERE ({where_sql})
        ORDER BY r.name
        LIMIT :limit
    """

    try:
        rows = db.execute(text(sql), params).fetchall()
        out = []
        for rid, rname, rcal in rows:
            out.append({
                "id": int(rid),
                "name": str(rname),
                "calories": rcal,
                "ingredients": get_ingredients_for_recipe(int(rid), db)
            })
        return out
    except Exception:
        return []


# =========================
# DEMO Intent helpers
# =========================

def detect_meal_type(q: str) -> Optional[str]:
    t = normalize_text(q)
    if "doruč" in t or "doruc" in t:
        return "doručak"
    if "ruč" in t or "ruc" in t:
        return "ručak"
    if "večer" in t or "vecer" in t:
        return "večera"
    return None

def is_idea_question(q: str) -> bool:
    t = normalize_text(q)
    triggers = ["što da jedem", "sta da jedem", "što skuhati", "sta skuhati", "ideja", "preporuči", "preporuci"]
    return any(x in t for x in triggers)

def detect_diet_flags(q: str) -> Dict[str, bool]:
    t = normalize_text(q)
    return {
        "low_cal": any(x in t for x in ["pazim na kalor", "manje kalor", "dijeta", "mršav", "mrsav", "cut", "deficit"]),
        "no_sugar": any(x in t for x in ["bez šećera", "bez secer", "no sugar", "ne jedem šećer", "ne jedem secer"]),
        "gluten_free": any(x in t for x in ["bez glutena", "gluten free", "celijak", "celijakija"]),
        "vegan": any(x in t for x in ["veganski", "vegan", "bez mesa", "bez životinj", "bez zivotinj"]),
        "vegetarian": any(x in t for x in ["vegetar", "bez mesa"]),
    }

def build_diet_advice(flags: Dict[str, bool]) -> str:
    lines = []
    if flags.get("low_cal"):
        lines.append("• Za manje kalorija: nemasni proteini + puno povrća, manje ulja, pečenje/kuhanje umjesto prženja.")
    if flags.get("no_sugar"):
        lines.append("• Bez šećera: izbjegavaj sokove i slatkiše, biraj jaja, meso, ribu, povrće, orašaste plodove.")
    if flags.get("gluten_free"):
        lines.append("• Bez glutena: biraj rižu, krumpir, kukuruz, povrće, meso, jaja; pazi na kruh/tjesteninu.")
    if flags.get("vegan"):
        lines.append("• Veganski: mahunarke, tofu, povrće, orašasti plodovi; dodaj izvor proteina u obrok.")
    if not lines:
        return ""
    return "Evo preporuka:\n" + "\n".join(lines)

def build_meal_ideas(meal_type: str, db: Session) -> str:
    """
    Tries to propose recipes from DB (if exist), otherwise gives generic ideas.
    """
    # Try DB-known demo words
    candidates = {
        "doručak": ["kajgana", "omlet", "zob", "palačinke", "palacinke"],
        "ručak": ["pileća", "piletina", "salata", "pizza", "tjestenina", "rucak"],
        "večera": ["salata", "kajgana", "pizza", "tuna", "vecera"],
    }.get(meal_type, [])

    found: List[Dict] = []
    seen_ids = set()

    for term in candidates:
        if len(found) >= 3:
            break
        try:
            res = find_recipes_in_db(term, db, limit=3)
            for r in res:
                rid = r.get("id")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    found.append(r)
                if len(found) >= 3:
                    break
        except Exception:
            continue

    if found:
        out = f"Ideje za {meal_type} (iz baze):\n"
        for r in found:
            kcal = f" ({r['calories']} kcal)" if r.get("calories") is not None else ""
            if r.get("ingredients"):
                out += f"🔹 {r['name']}{kcal}\n   Sastojci: {r['ingredients']}\n"
            else:
                out += f"🔹 {r['name']}{kcal}\n"
        return out

    # Generic fallback
    if meal_type == "doručak":
        return "Za doručak: kajgana/omlet, zobene pahuljice ili smoothie (ovisno što voliš)."
    if meal_type == "ručak":
        return "Za ručak: neki protein + povrće (npr. piletina i salata) ili jelo iz baze (napiši 'recept ...')."
    if meal_type == "večera":
        return "Za večeru: laganije opcije poput salate s proteinom, jaja ili nešto jednostavno iz baze."
    return "Reci mi je li to doručak, ručak ili večera pa ću predložiti ideje."

def build_health_answer(food: str) -> str:
    f = normalize_text(food)
    if not f:
        return "Možeš napisati: 'je li pizza zdrava' ili 'je li kajgana zdrava'."

    if "pizza" in f:
        return (
            "Pizza može biti OK povremeno 🙂\n"
            "Za zdraviju verziju: manje sira, više povrća i nemasni proteini, te tanje tijesto."
        )
    if "kajgana" in f or "jaja" in f or "jaje" in f:
        return (
            "Kajgana je uglavnom dobra jer ima proteine.\n"
            "Zdravija je ako se radi s malo ulja/maslaca i uz salatu ili povrće."
        )

    return (
        f"{food.capitalize()} može biti zdravo ovisno o količini i načinu pripreme.\n"
        "Ako je prženo i masno → češće nije idealno; ako je pečeno/kuhano i uz povrće → bolja opcija."
    )


# =========================
# Chat Logic
# =========================

def get_cooking_tip(query: str, db: Session) -> str:
    q = normalize_text(query)

    # 0) "što sve možeš" (meta)
    if any(x in q for x in ["što sve možeš", "sta sve mozes", "što možeš", "sta mozes", "kako mi možeš pomoći", "kako mi mozes pomoci"]):
        return (
            "Mogu ti pomoći ovako:\n"
            "• Recepti iz baze (upiši: 'recept pizza' ili 'možeš li mi napisati recept kajgana')\n"
            "• Sastojci recepta (automatski se prikažu ako postoje u bazi)\n"
            "• Zamjene sastojaka (npr. 'zamjena za mlijeko')\n"
            "• Ideje za doručak/ručak/večeru (npr. 'ideja za večeru')\n"
            "• Ako napišeš 'imam piletinu i jaja' predložit ću recepte po sastojcima\n"
            "• Savjeti za dijetu / kalorije / bez šećera / bez glutena\n"
            "• Odgovor na 'je li X zdravo'"
        )

    # 1) dijeta/kalorije/bez šećera/bez glutena/veganski
    flags = detect_diet_flags(q)
    diet_text = build_diet_advice(flags)
    # ako je korisnik pitao nešto u tom kontekstu, odgovori odmah
    if diet_text and any(x in q for x in ["što da jedem", "sta da jedem", "preporuči", "preporuci", "ideja", "što skuhati", "sta skuhati", "doruč", "doruc", "ruč", "ruc", "večer", "vecer"]):
        # plus meal idea if meal type present
        meal_type = detect_meal_type(q)
        if meal_type:
            return diet_text + "\n\n" + build_meal_ideas(meal_type, db)
        return diet_text

    # 2) greeting
    if any(x in q for x in ["bok", "pozdrav", "zdravo", "hej", "dobar dan", "dobro jutro", "dobra večer", "dobra vecer"]):
        return "Tu sam! Probaj: 'zamjena za mlijeko', 'ideja za večeru', 'imam piletinu i jaja', ili 'možeš li mi napisati recept pizza'."

    # 3) thanks
    if any(x in q for x in ["hvala", "super", "odlično", "odlicno", "bravo", "top"]):
        return "Nema na čemu! 😊"

    # 4) zdravlje pitanja: "je li X zdravo"
    if ("zdrav" in q) and ("je li" in q or "jel" in q):
        food = extract_health_food(q)
        return build_health_answer(food)

    # 5) ideje za obrok (doručak/ručak/večera)
    meal_type = detect_meal_type(q)
    if meal_type and (is_idea_question(q) or "za " in q or "ideja" in q):
        # ako i dijeta flag postoji, dodaj i to
        if diet_text:
            return diet_text + "\n\n" + build_meal_ideas(meal_type, db)
        return build_meal_ideas(meal_type, db)

    # 6) "imam ..." -> preporuči po sastojcima (JOIN)
    if "imam" in q:
        toks = extract_ingredients_after_imam(q)
        recipes = find_recipes_by_ingredient_tokens(toks, db, limit=3)
        if recipes:
            out = "Na temelju sastojaka koje imaš, možeš napraviti:\n"
            for r in recipes:
                kcal = f" ({r['calories']} kcal)" if r.get("calories") is not None else ""
                if r.get("ingredients"):
                    out += f"🔹 {r['name']}{kcal}\n   Sastojci: {r['ingredients']}\n"
                else:
                    out += f"🔹 {r['name']}{kcal}\n"
            return out
        # fallback bez baze
        if toks:
            return f"Imam zapisano da imaš: {', '.join(toks)}. Ako želiš recept iz baze, napiši 'recept <naziv>'."
        return "Napiši što imaš (npr. 'imam piletinu i jaja') pa ću predložiti recepte."

    # 7) recipe intent ANYWHERE in sentence
    if "recept" in q:
        term = extract_recipe_from_sentence(q)
        if not term:
            term = _clean_recipe_query(q)

        recipes = find_recipes_in_db(term, db, limit=3)
        if recipes:
            out = "Evo što imam u bazi:\n"
            for r in recipes:
                kcal = f" ({r['calories']} kcal)" if r.get("calories") is not None else ""
                if r.get("ingredients"):
                    out += f"🔹 {r['name']}{kcal}\n   Sastojci: {r['ingredients']}\n"
                else:
                    out += f"🔹 {r['name']}{kcal}\n"
            return out

        return "Ne mogu pronaći taj recept u bazi. Probaj: 'recept pizza', 'recept kajgana' ili 'recept pileća salata'."

    # 8) "što da jedem" bez meal type -> generički + (ako dijeta postoji već gore bi ušlo)
    if is_idea_question(q):
        return (
            "Reci mi je li to doručak, ručak ili večera 🙂\n"
            "Primjer: 'što da jedem za večeru' ili 'ideja za doručak'."
        )

    # fallback
    return "Tu sam! Probaj: 'zamjena za mlijeko', 'ideja za večeru', 'imam piletinu i jaja', ili 'možeš li mi napisati recept pizza'."


# =========================
# WebSocket Handler
# =========================

async def handle_websocket_message(websocket: WebSocket, user_id: str, message: Dict):
    message_type = message.get("type", "chat")
    content = message.get("content", "")

    db = get_db_session()
    try:
        if message_type == "chat":
            # 1) substitution inside chat
            ing = extract_ingredient_from_text(content)
            if ing:
                subs = get_ingredient_substitutions(ing)
                await manager.send_personal_message(json.dumps({
                    "type": "substitutions",
                    "ingredient": ing,
                    "substitutions": subs
                }), websocket)
                return

            # 2) only ingredient word -> treat as substitution (simple list)
            only_word = normalize_text(content)
            if only_word in ["mlijeko","mlijeka","maslac","šećer","secer","jaje","jaja","brašno","brasno","milk","butter","sugar","egg","flour"]:
                subs = get_ingredient_substitutions(only_word)
                await manager.send_personal_message(json.dumps({
                    "type": "substitutions",
                    "ingredient": only_word,
                    "substitutions": subs
                }), websocket)
                return

            # 3) normal response
            response = get_cooking_tip(content, db)
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
