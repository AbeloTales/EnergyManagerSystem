import sqlite3

def migrar_db_v4():
    print("Actualizando a V4 (Configuración y Telegram)...")
    conn = sqlite3.connect('energia.db')
    c = conn.cursor()

    # Tabla de Configuración Global
    c.execute('''
        CREATE TABLE IF NOT EXISTS config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            set_point_dinero REAL DEFAULT 20.0,  -- Presupuesto mensual en Dólares
            telegram_token TEXT DEFAULT '',
            telegram_chat_id TEXT DEFAULT '',
            ultima_alerta TEXT DEFAULT ''        -- Para no spammear mensajes
        )
    ''')
    
    # Inicializar con valores por defecto si está vacía
    c.execute("INSERT OR IGNORE INTO config (id, set_point_dinero) VALUES (1, 20.0)")

    conn.commit()
    conn.close()
    print("✅ Base de datos lista para Usuario y Telegram.")

if __name__ == "__main__":
    migrar_db_v4()