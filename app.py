import sqlite3
import cv2
import pytesseract
import RPi.GPIO as GPIO
import time
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import numpy as np # Necesario para calculos rapidos (pip install numpy)

app = Flask(__name__)

DB_NAME = 'energia.db'
CONSUMO_LIMITE_ALERTA = 5.0 # kWh (Si el DELTA supera esto, apagamos cargas bajas)

# Configuración OCR
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
        GPIO.output(pin, not estado) # Lógica inversa
    conn.close()

# --- CEREBRO: AUTOMATIZACIÓN (HORARIOS Y PRIORIDAD) ---
def verificar_inteligencia():
    print(f"[{datetime.now().strftime('%H:%M')}] Verificando reglas inteligentes...")
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    hora_actual = datetime.now().strftime("%H:%M")

    # 1. CONTROL POR HORARIO
    c.execute("SELECT * FROM grupos WHERE usar_horario = 1")
    grupos_horario = c.fetchall()
    
    for grupo in grupos_horario:
        inicio = grupo['hora_inicio']
        fin = grupo['hora_fin']
        id_grupo = grupo['id']
        
        # Lógica de intervalo (ej: 18:00 a 06:00 cruza medianoche)
        encender = False
        if inicio < fin: # Ej: 08:00 a 20:00
            if inicio <= hora_actual <= fin: encender = True
        else: # Ej: 20:00 a 06:00 (Cruza medianoche)
            if hora_actual >= inicio or hora_actual <= fin: encender = True
        
        # Aplicar cambio a los relés del grupo
        nuevo_estado = 1 if encender else 0
        
        # Solo actuamos si el estado actual es diferente para no saturar GPIO
        # (Simplificado: Forzamos actualización)
        c.execute("UPDATE reles SET estado=? WHERE id_grupo=?", (nuevo_estado, id_grupo))
        
        c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (id_grupo,))
        pines = c.fetchall()
        for p in pines:
            GPIO.output(p['pin_gpio'], not nuevo_estado)
            
    conn.commit()
    conn.close()

def gestion_carga_critica(ultimo_consumo):
    """Si el consumo es muy alto, apagar grupos de BAJA prioridad"""
    if ultimo_consumo > CONSUMO_LIMITE_ALERTA:
        print(f"⚠️ ALERTA: Consumo alto ({ultimo_consumo}). Apagando no prioritarios.")
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        # Buscar grupos con prioridad BAJA (0)
        c.execute("SELECT id FROM grupos WHERE prioridad = 0")
        grupos_baja = c.fetchall()
        
        for g in grupos_baja:
            id_grupo = g[0]
            # Apagar relés de este grupo
            c.execute("UPDATE reles SET estado=0 WHERE id_grupo=?", (id_grupo,))
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (id_grupo,))
            pines = c.fetchall()
            for p in pines:
                GPIO.output(p[0], True) # True es APAGADO (inverso)
        
        conn.commit()
        conn.close()

# --- TAREA OCR (MONITOREO) ---
def tarea_monitoreo_energia():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened(): return
    cap.set(3, 640); cap.set(4, 480)
    ret, frame = cap.read()
    cap.release()

    if ret:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        try:
            texto = pytesseract.image_to_string(thresh, config=config_tesseract)
            texto_limpio = ''.join(filter(str.isdigit, texto))
            
            if len(texto_limpio) > 0:
                lectura_actual = float(texto_limpio)
                
                # CÁLCULO DIFERENCIAL (Consumo Real)
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                
                # Obtener la lectura ANTERIOR
                c.execute("SELECT valor_kwh FROM lecturas ORDER BY id DESC LIMIT 1")
                ultima = c.fetchone()
                
                consumo_delta = 0.0
                if ultima:
                    lectura_anterior = ultima[0]
                    # Solo si la nueva es mayor (para evitar errores de reset o mala lectura)
                    if lectura_actual >= lectura_anterior:
                        consumo_delta = lectura_actual - lectura_anterior
                    else:
                         # Filtro de ruido: Si es menor, ignoramos o asumimos 0
                         consumo_delta = 0 
                
                # Guardar ambos datos
                c.execute("INSERT INTO lecturas (valor_kwh, consumo_delta) VALUES (?, ?)", 
                          (lectura_actual, consumo_delta))
                conn.commit()
                conn.close()
                
                print(f"Lectura: {lectura_actual} | Consumo detectado: {consumo_delta}")
                
                # Ejecutar reglas de apagado por exceso
                gestion_carga_critica(consumo_delta)

        except Exception as e: print(f"Error OCR: {e}")

# --- API Y RUTAS ---

@app.route('/')
def index(): return render_template('dashboard.html')

@app.route('/api/datos')
def api_datos():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. Gráfico: Enviamos el DELTA (Consumo), no el acumulado
    c.execute("SELECT fecha, consumo_delta FROM lecturas ORDER BY id DESC LIMIT 20")
    raw_data = c.fetchall()
    grafico = [list(row) for row in raw_data][::-1] # Invertir para cronológico
    
    # 2. Predicción y Promedios (Últimos 10 datos)
    consumos = [x[1] for x in grafico[-10:]] if grafico else [0]
    promedio = sum(consumos) / len(consumos) if consumos else 0
    # Predicción simplificada: Promedio * 30 días (ajustar según frecuencia de lectura)
    # Si lees cada 1 min, esto sería proyección de 30 mins. 
    # Para mes real necesitamos saber intervalos. Asumamos proyección simple.
    prediccion_mes = promedio * 30 * 24 # Ejemplo burdo
    
    alerta = "Normal"
    if consumos and consumos[-1] > (promedio * 1.2): alerta = "Subiendo"
    elif consumos and consumos[-1] < (promedio * 0.8): alerta = "Bajando"

    # 3. Datos del Sistema
    c.execute("SELECT * FROM reles")
    reles = [dict(row) for row in c.fetchall()]
    c.execute("SELECT * FROM grupos")
    grupos = [dict(row) for row in c.fetchall()]
    
    # 4. Historial (Tabla)
    c.execute("SELECT id, fecha, valor_kwh, consumo_delta FROM lecturas ORDER BY id DESC LIMIT 50")
    historial = [dict(row) for row in c.fetchall()]

    conn.close()
    
    return jsonify({
        'grafico': grafico,
        'reles': reles,
        'grupos': grupos,
        'estadisticas': {
            'promedio': round(promedio, 2),
            'tendencia': alerta,
            'prediccion': round(prediccion_mes, 2)
        },
        'historial': historial
    })

@app.route('/api/control', methods=['POST'])
def api_control():
    data = request.json
    accion = data.get('accion')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    try:
        if accion == 'toggle':
            id_rele = data.get('id')
            c.execute("SELECT estado, pin_gpio FROM reles WHERE id=?", (id_rele,))
            r = c.fetchone()
            if r:
                nuevo = 1 - r[0]
                GPIO.output(r[1], not nuevo)
                c.execute("UPDATE reles SET estado=? WHERE id=?", (nuevo, id_rele))

        elif accion == 'crear_grupo': # Ahora guarda prioridad y horarios
            nombre = data.get('nombre')
            prio = int(data.get('prioridad')) # 1 o 0
            horario = int(data.get('usar_horario')) # 1 o 0
            inicio = data.get('hora_inicio')
            fin = data.get('hora_fin')
            ids = data.get('reles')
            
            c.execute("INSERT INTO grupos (nombre, prioridad, usar_horario, hora_inicio, hora_fin) VALUES (?,?,?,?,?)",
                      (nombre, prio, horario, inicio, fin))
            gid = c.lastrowid
            for rid in ids:
                c.execute("UPDATE reles SET id_grupo=? WHERE id=?", (gid, rid))
                
        elif accion == 'eliminar_grupo':
             gid = data.get('id_grupo')
             c.execute("UPDATE reles SET id_grupo=0 WHERE id_grupo=?", (gid,))
             c.execute("DELETE FROM grupos WHERE id=?", (gid,))

        # ... (Mantener lógica de control global si se desea) ...

        conn.commit()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    setup_gpio()
    sched = BackgroundScheduler()
    sched.add_job(tarea_monitoreo_energia, 'interval', minutes=1) # Lectura cada minuto
    sched.add_job(verificar_inteligencia, 'interval', minutes=1) # Chequeo horario cada minuto
    sched.start()
    app.run(host='0.0.0.0', port=80, debug=False, use_reloader=False)