import sqlite3
import cv2
import pytesseract
import RPi.GPIO as GPIO
import time
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURACIÓN ---
DB_NAME = 'energia.db'
LIMITE_CONSUMO_ALERTA = 5000.0  # Si lee más de esto, se apaga todo (ejemplo)

# Configuración OCR (Solo números)
config_tesseract = r'--oem 3 --psm 6 outputbase digits'

# --- GESTIÓN DE HARDWARE ---
def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT pin_gpio, estado FROM reles")
    for row in c.fetchall():
        pin, estado = row
        GPIO.setup(pin, GPIO.OUT)
        # Lógica inversa (si tu relé se activa con LOW/False)
        # Si estado DB es 1 (ON) -> GPIO LOW
        # Si estado DB es 0 (OFF) -> GPIO HIGH
        GPIO.output(pin, not estado) 
    conn.close()

# --- TAREA DE FONDO: LECTURA DE CÁMARA (OCR) ---
# Esta función corre sola cada X minutos
def tarea_monitoreo_energia():
    print(f"[{datetime.now()}] Iniciando lectura de medidor...")
    
    # 1. Captura de imagen (Optimizada)
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Error: Cámara no detectada.")
        return
    
    # Ajustamos resolución baja para velocidad (640x480 es suficiente para OCR)
    cap.set(3, 640)
    cap.set(4, 480)
    ret, frame = cap.read()
    cap.release() # Liberar cámara inmediatamente

    valor_leido = 0.0

    if ret:
        # Pre-procesamiento para mejorar lectura
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Umbralización (blanco y negro puro)
        thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]

        try:
            texto = pytesseract.image_to_string(thresh, config=config_tesseract)
            # Limpieza de string (quitar letras, espacios)
            texto_limpio = ''.join(filter(str.isdigit, texto))
            
            if len(texto_limpio) > 0:
                valor_leido = float(texto_limpio)
                print(f"Lectura OCR: {valor_leido} kWh")
                
                # GUARDAR EN BD
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("INSERT INTO lecturas (valor_kwh) VALUES (?)", (valor_leido,))
                conn.commit()
                conn.close()
                
                # VERIFICACIÓN DE SEGURIDAD (Auto-Apagado)
                verificar_limites(valor_leido)
            else:
                print("No se pudieron leer dígitos claros.")
                
        except Exception as e:
            print(f"Error procesando OCR: {e}")

def verificar_limites(valor_actual):
    """Si el consumo es absurdo o peligroso, apagar todo"""
    if valor_actual > LIMITE_CONSUMO_ALERTA:
        print("¡ALERTA! Consumo excede límite. Apagando todo...")
        apagar_todos_los_reles()

def apagar_todos_los_reles():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Poner todos en 0 en la BD
    c.execute("UPDATE reles SET estado = 0")
    conn.commit()
    
    # Actualizar físicamente
    c.execute("SELECT pin_gpio FROM reles")
    for row in c.fetchall():
        GPIO.output(row[0], True) # True es APAGADO en relé inverso
    conn.close()

# --- RUTAS WEB (FLASK) ---

@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/api/datos')
def api_datos():
    """Devuelve datos para el gráfico y estado de relés"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row 
    c = conn.cursor()
    
    # 1. Obtener últimas 20 lecturas
    c.execute("SELECT fecha, valor_kwh FROM lecturas ORDER BY id DESC LIMIT 20")
    # --- CORRECCIÓN AQUÍ: Convertimos cada fila 'Row' a una lista normal ---
    rows = c.fetchall()
    lecturas = [list(row) for row in rows] 
    
    # 2. Obtener estado de relés
    c.execute("SELECT * FROM reles")
    # Convertimos también los relés a diccionarios
    reles = [dict(row) for row in c.fetchall()]
    
    # 3. Obtener grupos
    c.execute("SELECT * FROM grupos")
    grupos = [dict(row) for row in c.fetchall()]
    
    conn.close()
    
    return jsonify({
        'grafico': lecturas[::-1], 
        'reles': reles,
        'grupos': grupos
    })

@app.route('/api/control', methods=['POST'])
def api_control():
    data = request.json
    accion = data.get('accion')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    try:
        # --- 1. CONTROL INDIVIDUAL (Toggle) ---
        if accion == 'toggle':
            id_rele = data.get('id')
            c.execute("SELECT estado, pin_gpio FROM reles WHERE id=?", (id_rele,))
            fila = c.fetchone()
            if fila:
                nuevo_estado = 1 if fila[0] == 0 else 0
                GPIO.output(fila[1], not nuevo_estado) # Recordar lógica inversa
                c.execute("UPDATE reles SET estado=? WHERE id=?", (nuevo_estado, id_rele))

        # --- 2. EDITAR NOMBRE RELÉ ---
        elif accion == 'editar_nombre':
            id_rele = data.get('id')
            nombre = data.get('nombre')
            c.execute("UPDATE reles SET nombre=? WHERE id=?", (nombre, id_rele))

        # --- 3. CONTROL GLOBAL (MASTER SWITCH) ---
        elif accion == 'global':
            # estado_obj: 1 para encender todo, 0 para apagar todo
            estado_obj = int(data.get('estado')) 
            # Actualizamos BD
            c.execute("UPDATE reles SET estado=?", (estado_obj,))
            # Actualizamos Físico
            c.execute("SELECT pin_gpio FROM reles")
            for row in c.fetchall():
                # Lógica inversa: Si quiero Encender (1), envío False (0)
                GPIO.output(row[0], not estado_obj)

        # --- 4. CONTROL DE GRUPO ---
        elif accion == 'grupo':
            id_grupo = data.get('id_grupo')
            estado_obj = int(data.get('estado')) # 1 o 0
            
            # Buscamos los pines de ese grupo
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (id_grupo,))
            pines = c.fetchall()
            
            if pines:
                # Actualizamos BD para ese grupo
                c.execute("UPDATE reles SET estado=? WHERE id_grupo=?", (estado_obj, id_grupo))
                # Actualizamos Físico
                for row in pines:
                    GPIO.output(row[0], not estado_obj)

        # --- 5. CREAR NUEVO GRUPO ---
        elif accion == 'crear_grupo':
            nombre_grupo = data.get('nombre')
            ids_reles = data.get('reles') # Lista de IDs ej: [1, 3, 5]
            
            # 1. Crear el grupo en tabla 'grupos'
            c.execute("INSERT INTO grupos (nombre) VALUES (?)", (nombre_grupo,))
            nuevo_id_grupo = c.lastrowid
            
            # 2. Asignar los relés seleccionados a este grupo
            # Primero los limpiamos de grupos anteriores (opcional, pero recomendado)
            # Luego asignamos el nuevo
            for id_r in ids_reles:
                c.execute("UPDATE reles SET id_grupo=? WHERE id=?", (nuevo_id_grupo, id_r))

        # --- 6. ELIMINAR GRUPO ---
        elif accion == 'eliminar_grupo':
            id_grupo = data.get('id_grupo')
            # Liberamos los relés (id_grupo = 0)
            c.execute("UPDATE reles SET id_grupo=0 WHERE id_grupo=?", (id_grupo,))
            # Borramos el grupo
            c.execute("DELETE FROM grupos WHERE id=?", (id_grupo,))

        conn.commit()
        return jsonify({'status': 'ok'})
        
    except Exception as e:
        return jsonify({'status': 'error', 'mensaje': str(e)}), 500
    finally:
        conn.close()

# --- INICIO DEL SISTEMA ---
if __name__ == '__main__':
    try:
        setup_gpio()
        
        # Iniciamos el Scheduler (El reloj interno)
        scheduler = BackgroundScheduler()
        # Ejecuta la tarea de cámara cada 60 segundos (ajustable)
        scheduler.add_job(tarea_monitoreo_energia, 'interval', seconds=60)
        scheduler.start()
        
        # Iniciamos Web Server
        # use_reloader=False es necesario cuando usas Scheduler para que no se duplique
        app.run(host='0.0.0.0', port=80, debug=True, use_reloader=False)
        
    except KeyboardInterrupt:
        print("Apagando sistema...")
        GPIO.cleanup()
