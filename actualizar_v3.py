import sqlite3

def migrar_db():
    print("Actualizando base de datos a V3 (Modo Cíclico)...")
    conn = sqlite3.connect('energia.db')
    c = conn.cursor()

    col_nuevas = [
        ("modo_ciclo", "INTEGER DEFAULT 0"),   # 1 = Activado
        ("ciclo_on", "INTEGER DEFAULT 0"),     # Minutos encendido
        ("ciclo_off", "INTEGER DEFAULT 0"),    # Minutos apagado
        ("ultima_accion", "TEXT")              # Fecha/Hora del último cambio
    ]

    for nombre, tipo in col_nuevas:
        try:
            c.execute(f"ALTER TABLE grupos ADD COLUMN {nombre} {tipo}")
            print(f"Columna '{nombre}' agregada.")
        except:
            print(f"Columna '{nombre}' ya existía.")

    conn.commit()
    conn.close()
    print("✅ Base de datos actualizada.")

if __name__ == "__main__":
    migrar_db()