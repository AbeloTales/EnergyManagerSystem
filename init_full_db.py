import sqlite3
import os

DB_NAME = 'energia.db'

def inicializar_db():
    print(f"Inicializando base de datos: {DB_NAME}...")
    
    # Nos conectamos (esto crea el archivo si no existe)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # --- 1. TABLA LECTURAS (Con soporte para Deltas) ---
    c.execute('''
        CREATE TABLE IF NOT EXISTS lecturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            valor_kwh REAL,
            consumo_delta REAL DEFAULT 0
        )
    ''')

    # --- 2. TABLA GRUPOS (Con soporte Inteligente) ---
    c.execute('''
        CREATE TABLE IF NOT EXISTS grupos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT,
            prioridad INTEGER DEFAULT 1,     -- 1: Alta, 0: Baja
            usar_horario INTEGER DEFAULT 0,  -- 1: Sí, 0: No
            hora_inicio TEXT DEFAULT '18:00',
            hora_fin TEXT DEFAULT '06:00'
        )
    ''')

    # --- 3. TABLA RELES ---
    c.execute('''
        CREATE TABLE IF NOT EXISTS reles (
            id INTEGER PRIMARY KEY,
            pin_gpio INTEGER,
            nombre TEXT,
            id_grupo INTEGER DEFAULT 0,
            estado INTEGER DEFAULT 0,
            consumo_estimado REAL DEFAULT 0
        )
    ''')

    # --- 4. SEMILLA DE DATOS (Si la tabla reles está vacía) ---
    c.execute("SELECT count(*) FROM reles")
    if c.fetchone()[0] == 0:
        print("Creando los 8 relés por defecto...")
        pines = [17, 27, 22, 23, 24, 25, 5, 6] # Tus pines
        for i, pin in enumerate(pines):
            # Insertamos relé por defecto asignado al grupo 0 (Sin grupo)
            c.execute("INSERT INTO reles (id, pin_gpio, nombre, id_grupo, estado) VALUES (?, ?, ?, 0, 0)", 
                      (i+1, pin, f"Relé {i+1}"))
    
    # Crear un grupo por defecto si no existe
    c.execute("INSERT OR IGNORE INTO grupos (id, nombre, prioridad) VALUES (1, 'General', 1)")

    conn.commit()
    conn.close()
    print("✅ ¡Base de datos creada y lista para el Sistema Inteligente!")

if __name__ == "__main__":
    # Aseguramos que se ejecute en la carpeta correcta
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    inicializar_db()
