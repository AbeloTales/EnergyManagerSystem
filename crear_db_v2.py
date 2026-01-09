import sqlite3

def actualizar_base_datos():
    conn = sqlite3.connect('energia.db')
    c = conn.cursor()

    # 1. Tabla de Lecturas: Agregamos columna para el consumo calculado (Diferencia)
    try:
        c.execute("ALTER TABLE lecturas ADD COLUMN consumo_delta REAL DEFAULT 0")
    except: pass # Si ya existe, ignorar

    # 2. Tabla de Grupos: Agregamos inteligencia (Prioridad y Horarios)
    # prioridad: 1 = Alta (No apagar), 0 = Baja (Apagable por consumo)
    # usar_horario: 1 = Sí, 0 = No
    cols = [
        ("prioridad", "INTEGER DEFAULT 1"),
        ("usar_horario", "INTEGER DEFAULT 0"),
        ("hora_inicio", "TEXT DEFAULT '18:00'"),
        ("hora_fin", "TEXT DEFAULT '06:00'")
    ]
    for col, tipo in cols:
        try:
            c.execute(f"ALTER TABLE grupos ADD COLUMN {col} {tipo}")
        except: pass

    conn.commit()
    conn.close()
    print("Base de datos actualizada a Versión Inteligente.")

if __name__ == "__main__":
    # Si no tienes la DB creada aun, usa el script anterior primero, o corre esto:
    # (Aquí asumimos que la DB básica ya existe del paso anterior)
    actualizar_base_datos()