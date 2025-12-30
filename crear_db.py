import sqlite3

def crear_base_datos():
    conn = sqlite3.connect('energia.db')
    c = conn.cursor()

    # 1. Tabla de Lecturas (El histórico de consumo)
    # Guardamos fecha (texto ISO) y valor leído
    c.execute('''
        CREATE TABLE IF NOT EXISTS lecturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            valor_kwh REAL
        )
    ''')

    # 2. Tabla de Relés (Configuración física y lógica)
    # estado: 0 apagado, 1 encendido
    # consumo_estimado: cuánto gasta lo que está conectado ahí (en Watts)
    c.execute('''
        CREATE TABLE IF NOT EXISTS reles (
            id INTEGER PRIMARY KEY, -- Del 1 al 8 (físico)
            pin_gpio INTEGER,
            nombre TEXT,
            id_grupo INTEGER,
            estado INTEGER DEFAULT 0,
            consumo_estimado REAL DEFAULT 0
        )
    ''')

    # 3. Tabla de Grupos (Para agrupar relés)
    c.execute('''
        CREATE TABLE IF NOT EXISTS grupos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT
        )
    ''')

    # --- DATOS INICIALES (SEMILLA) ---
    # Insertamos los 8 relés por defecto si no existen
    # Pines BCM según tu esquema anterior: [17, 27, 22, 23, 24, 25, 5, 6]
    pines = [17, 27, 22, 23, 24, 25, 5, 6]
    c.execute("SELECT count(*) FROM reles")
    if c.fetchone()[0] == 0:
        print("Creando relés por defecto...")
        for i, pin in enumerate(pines):
            # ID (1-8), PIN, NOMBRE, ID_GRUPO(0=Sin grupo), ESTADO, CONSUMO
            c.execute("INSERT INTO reles VALUES (?, ?, ?, ?, ?, ?)", 
                      (i+1, pin, f"Relé {i+1}", 0, 0, 100.0))
    
    # Creamos un grupo por defecto
    c.execute("INSERT OR IGNORE INTO grupos (id, nombre) VALUES (1, 'General')")

    conn.commit()
    conn.close()
    print("Base de datos 'energia.db' creada/actualizada correctamente.")

if __name__ == "__main__":
    crear_base_datos()
