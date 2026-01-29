# SmartMeal AI - Python AI Modul i WebSocket Dokumentacija

## 🚀 Što je implementirano

### Python AI Modul (Port 8001)

#### 1. Napredni Recommendation Engine

**Endpoint:** `POST /recommend-meals/`

**Funkcionalnosti:**
- ✅ **Content-Based Filtering** - preporuke temeljene na sličnosti recepata
- ✅ **Collaborative Filtering** - preporuke temeljene na sličnim korisnicima
- ✅ **Kalkulacija BMR/TDEE** - automatski izračun kalorijskih potreba
- ✅ **Optimizacija prema ciljevima** - prilagodba za mršavljenje/povećanje mase/održavanje
- ✅ **Filtriranje po alergijama** - isključivanje recepata s alergenima
- ✅ **Filtriranje po dostupnim namirnicama** - preferiranje recepata s dostupnim sastojcima
- ✅ **Weighted Scoring System** - kombinacija svih faktora za najbolje preporuke

**Primjer zahtjeva:**
```json
{
  "user_id": 1,
  "age": 30,
  "gender": "male",
  "weight": 80,
  "height": 180,
  "activity_level": "moderate",
  "preferences": "healthy vegetarian low-carb",
  "goals": {
    "type": "weight_loss",
    "target_calories": 1800
  },
  "inventory": ["tofu", "spinach", "olive oil"],
  "diet_type": "vegetarian",
  "allergies": ["peanuts", "shellfish"]
}
```

**Primjer odgovora:**
```json
{
  "recommendations": [
    {
      "id": 1,
      "name": "Tofu and Spinach Salad",
      "calories": 350,
      "protein": 25,
      "carbs": 15,
      "fat": 20,
      "prep_time": 15
    }
  ],
  "daily_calorie_target": 2200,
  "macros": {
    "protein": 192.5,
    "carbs": 220.0,
    "fat": 61.1
  },
  "explanation": "Based on your weight_loss goal, we recommend 5 meals. Your daily calorie target is 2200 kcal. Target macros: 192g protein, 220g carbs, 61g fat."
}
```

#### 2. WebSocket AI Assistant

**Endpoint:** `WS /ws/assistant/{user_id}`

**Funkcionalnosti:**
- ✅ **Real-time chat** - razgovor s AI asistentom
- ✅ **Zamjene sastojaka** - predlaganje alternativa za sastojke
- ✅ **Vođenje kroz pripremu** - step-by-step upute za kuhanje
- ✅ **Savjeti za kuhanje** - kako smanjiti kalorije, povećati proteine, itd.
- ✅ **Kalkulacija prilagođene nutritivne vrijednosti** - nakon zamjena sastojaka

**Tipovi poruka:**

1. **Chat poruka**
```json
{
  "type": "chat",
  "content": "How can I reduce calories in this recipe?"
}
```

2. **Zamjena sastojka**
```json
{
  "type": "substitute",
  "ingredient": "butter"
}
```

Odgovor:
```json
{
  "type": "substitutions",
  "ingredient": "butter",
  "substitutions": [
    {
      "substitute": "olive oil",
      "ratio": "3/4 cup oil per 1 cup butter",
      "note": "Healthier fats"
    },
    {
      "substitute": "applesauce",
      "ratio": "1:1",
      "note": "Lower calories, for baking"
    }
  ]
}
```

3. **Početak kuhanja**
```json
{
  "type": "recipe_start",
  "recipe": {
    "name": "Tofu Salad",
    "steps": [
      "Cut tofu into cubes",
      "Wash spinach",
      "Mix with olive oil",
      "Season and serve"
    ]
  }
}
```

4. **Sljedeći korak**
```json
{
  "type": "next_step"
}
```

Odgovor:
```json
{
  "type": "step",
  "step_number": 1,
  "total_steps": 4,
  "instruction": "Cut tofu into cubes"
}
```

5. **Prilagodba nutritivne vrijednosti**
```json
{
  "type": "nutrition_adjust",
  "recipe": {
    "calories": 500,
    "protein": 20,
    "carbs": 60,
    "fat": 15
  },
  "substitutions": [
    {"note": "lower calories"},
    {"note": "high protein"}
  ]
}
```

---

## 🔧 Konfiguracija

### .env file
```env
# Database Configuration (SQLite)
DB_TYPE=sqlite
DB_PATH=../smartmeal/database/database.sqlite

# AI Configuration
OPENAI_API_KEY=your_openai_api_key_here

# Server Configuration
API_HOST=127.0.0.1
API_PORT=8001
```

---

## 🚀 Pokretanje

### 1. Instalacija paketa
```bash
cd SmartMealAI
pip install -r requirements.txt
```

### 2. Pokretanje servera
```bash
uvicorn main:app --host 127.0.0.1 --port 8001 --reload
```

### 3. Provjera statusa
```bash
curl http://127.0.0.1:8001/health
```

Očekivani odgovor:
```json
{
  "status": "healthy",
  "database": "connected",
  "version": "2.0.0"
}
```

---

## 📊 Algoritmi

### 1. BMR Kalkulacija (Mifflin-St Jeor Equation)
- **Muškarci:** BMR = (10 × težina) + (6.25 × visina) - (5 × dob) + 5
- **Žene:** BMR = (10 × težina) + (6.25 × visina) - (5 × dob) - 161

### 2. TDEE Kalkulacija
- **Sedentary:** BMR × 1.2
- **Light:** BMR × 1.375
- **Moderate:** BMR × 1.55
- **Active:** BMR × 1.725
- **Very Active:** BMR × 1.9

### 3. Kalorijski ciljevi
- **Weight Loss:** TDEE - 500 kcal
- **Muscle Gain:** TDEE + 300 kcal
- **Maintenance:** TDEE

### 4. Makro distribucija

**Weight Loss:**
- Proteini: 35% (visoko)
- Ugljikohidrati: 40% (umjereno)
- Masti: 25% (nisko)

**Muscle Gain:**
- Proteini: 30% (visoko)
- Ugljikohidrati: 45% (visoko)
- Masti: 25% (umjereno)

**Maintenance:**
- Proteini: 25% (balansirano)
- Ugljikohidrati: 45% (balansirano)
- Masti: 30% (balansirano)

### 5. Scoring System

Finalni score = 
- 40% × Content-Based Score (TF-IDF sličnost)
- 30% × Collaborative Score (popularnost među sličnim korisnicima)
- 20% × Ingredient Availability Score
- 10% × Calorie Match Score

---

## 🧪 Testiranje

### Test 1: Health Check
```bash
curl http://127.0.0.1:8001/health
```

### Test 2: Preporuke
```bash
curl -X POST http://127.0.0.1:8001/recommend-meals/ \
  -H "Content-Type: application/json" \
  -d '{
    "age": 30,
    "gender": "male",
    "weight": 80,
    "height": 180,
    "activity_level": "moderate",
    "preferences": "healthy vegetarian",
    "goals": {"type": "weight_loss"},
    "inventory": ["tofu", "spinach"],
    "allergies": []
  }'
```

### Test 3: WebSocket (JavaScript)
```javascript
const ws = new WebSocket('ws://127.0.0.1:8001/ws/assistant/user123');

ws.onopen = () => {
  console.log('Connected to AI Assistant');
  
  // Test chat
  ws.send(JSON.stringify({
    type: 'chat',
    content: 'How can I reduce calories?'
  }));
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Response:', data);
};
```

---

## 📁 Struktura projekta

```
SmartMealAI/
├── main.py                    # Glavni FastAPI server
├── database.py                # SQLAlchemy modeli i konekcija
├── websocket_assistant.py     # WebSocket AI asistent
├── requirements.txt           # Python paketi
├── .env                       # Konfiguracija
└── env/                       # Virtual environment
```

---

## 🔗 Integracija s Laravel backendom

Laravel backend može koristiti Python AI modul putem HTTP zahtjeva:

```php
// app/Http/Controllers/AIController.php
$response = Http::post('http://127.0.0.1:8001/recommend-meals/', [
    'age' => $request->age,
    'gender' => $request->gender,
    'weight' => $request->weight,
    'height' => $request->height,
    'activity_level' => $request->activity_level,
    'preferences' => $request->preferences,
    'goals' => $request->goals,
    'inventory' => $request->inventory,
]);
```

---

## ⚠️ Napomene

1. **Port 8001** - Python AI modul mora biti na drugom portu od Laravel backenda (8000)
2. **SQLite baza** - Koristi istu bazu kao Laravel backend (`../smartmeal/database/database.sqlite`)
3. **CORS** - Omogućen za sve origine (`allow_origins=["*"]`)
4. **Auto-reload** - Server se automatski restartuje pri promjenama koda

---

## 🎯 Sljedeći koraci

1. ✅ Python AI modul - GOTOVO
2. ✅ WebSocket AI asistent - GOTOVO
3. ⏳ Frontend WebSocket klijent - TREBA IMPLEMENTIRATI
4. ⏳ OpenAI API integracija - OPCIONALNO
5. ⏳ Testiranje end-to-end - TREBA TESTIRATI
