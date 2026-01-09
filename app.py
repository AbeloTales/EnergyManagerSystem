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
CONSUMO_LIMITE_ALERTA = 5.0 
config_tesseract = r'--oem 3 --psm 6 outputbase digits'

# --- GESTIÓN DE HARDWARE ---
def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("SELECT pin_gpio, estado FROM reles")
        for row in c.fetchall():
            pin, estado = row
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, not estado) 
    except: pass
    conn.close()

# --- CEREBRO INTELIGENTE ---
def verificar_inteligencia():
    """Revisa cada minuto si hay que encender/apagar por horario"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Hora actual en formato 24h (HH:MM)
    hora_actual = datetime.now().strftime("%H:%M")
    
    try:
        c.execute("SELECT * FROM grupos WHERE usar_horario = 1")
        grupos = c.fetchall()
        
        for g in grupos:
            inicio = g['hora_inicio']
            fin = g['hora_fin']
            gid = g['id']
            
            # Lógica de intervalo
            encender = False
            if inicio < fin: # Ej: 08:00 a 20:00
                if inicio <= hora_actual < fin: encender = True
            else: # Cruza medianoche (Ej: 22:00 a 06:00)
                if hora_actual >= inicio or hora_actual < fin: encender = True
            
            # Aplicar estado
            nuevo_estado = 1 if encender else 0
            
            # Solo actualizamos si cambia el estado (para no saturar logs, opcional)
            c.execute("UPDATE reles SET estado=? WHERE id_grupo=?", (nuevo_estado, gid))
            
            # Actualizamos físico
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (gid,))
            for r in c.fetchall():
                GPIO.output(r['pin_gpio'], not nuevo_estado)
        
        conn.commit()
    except Exception as e:
        print(f"Error inteligencia: {e}")
    finally:
        conn.close()

def gestion_carga_critica(consumo_actual):
    if consumo_actual > CONSUMO_LIMITE_ALERTA:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT id FROM grupos WHERE prioridad = 0")
        grupos_baja = c.fetchall()
        for g in grupos_baja:
            gid = g[0]
            c.execute("UPDATE reles SET estado=0 WHERE id_grupo=?", (gid,))
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (gid,))
            for r in c.fetchall():
                GPIO.output(r[0], True) # Apagar
        conn.commit()
        conn.close()

# --- TAREA OCR ---
def tarea_monitoreo_energia():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened(): return
    cap.set(3, 640); cap.set(4, 480)
    ret, frame = cap.read()
    cap.release()

    if ret:
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
            texto = pytesseract.image_to_string(thresh, config=config_tesseract)
            texto_limpio = ''.join(filter(str.isdigit, texto))
            
            if len(texto_limpio) > 0:
                lectura_actual = float(texto_limpio)
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("SELECT valor_kwh FROM lecturas ORDER BY id DESC LIMIT 1")
                ultima = c.fetchone()
                
                delta = 0.0
                if ultima:
                    anterior = ultima[0]
                    if lectura_actual >= anterior:
                        delta = lectura_actual - anterior
                
                c.execute("INSERT INTO lecturas (valor_kwh, consumo_delta) VALUES (?, ?)", 
                          (lectura_actual, delta))
                conn.commit()
                conn.close()
                gestion_carga_critica(delta)
        except Exception as e: pass

# --- API ---
@app.route('/')
def index(): return render_template('dashboard.html')

@app.route('/api/datos')
def api_datos():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Gráfico
    c.execute("SELECT fecha, consumo_delta FROM lecturas ORDER BY id DESC LIMIT 20")
    grafico = [list(row) for row in c.fetchall()][::-1]
    
    # Stats
    vals = [x[1] for x in grafico[-10:]] if grafico else [0]
    promedio = sum(vals) / len(vals) if vals else 0
    prediccion = promedio * 43200 
    
    tendencia = "Estable"
    if vals and vals[-1] > promedio * 1.1: tendencia = "Subiendo"
    elif vals and vals[-1] < promedio * 0.9: tendencia = "Bajando"

    # Datos
    c.execute("SELECT * FROM reles")
    reles = [dict(row) for row in c.fetchall()]
    c.execute("SELECT * FROM grupos")
    grupos = [dict(row) for row in c.fetchall()]
    c.execute("SELECT * FROM lecturas ORDER BY id DESC LIMIT 50")
    historial = [dict(row) for row in c.fetchall()]
    
    conn.close()
    
    # Enviamos hora del servidor para depurar horarios
    hora_servidor = datetime.now().strftime("%H:%M:%S")

    return jsonify({
        'grafico': grafico, 'reles': reles, 'grupos': grupos, 'historial': historial,
        'estadisticas': { 'promedio': round(promedio, 2), 'tendencia': tendencia, 'prediccion': round(prediccion, 2) },
        'hora_servidor': hora_servidor
    })

@app.route('/api/control', methods=['POST'])
def api_control():
    data = request.json
    accion = data.get('accion')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    try:
        if accion == 'toggle':
            rid = data.get('id')
            c.execute("SELECT estado, pin_gpio FROM reles WHERE id=?", (rid,))
            r = c.fetchone()
            if r:
                nuevo = 1 - r[0]
                GPIO.output(r[1], not nuevo)
                c.execute("UPDATE reles SET estado=? WHERE id=?", (nuevo, rid))
        
        elif accion == 'editar_nombre':
            c.execute("UPDATE reles SET nombre=? WHERE id=?", (data.get('nombre'), data.get('id')))

        elif accion == 'grupo':
            gid = int(data.get('id_grupo'))
            est = int(data.get('estado'))
            c.execute("UPDATE reles SET estado=? WHERE id_grupo=?", (est, gid))
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (gid,))
            for r in c.fetchall(): GPIO.output(r[0], not est)

        elif accion == 'global':
            est = int(data.get('estado'))
            c.execute("UPDATE reles SET estado=?", (est,))
            c.execute("SELECT pin_gpio FROM reles")
            for r in c.fetchall(): GPIO.output(r[0], not est)

        elif accion == 'crear_grupo':
            c.execute("INSERT INTO grupos (nombre, prioridad, usar_horario, hora_inicio, hora_fin) VALUES (?,?,?,?,?)",
                      (data.get('nombre'), int(data.get('prioridad')), int(data.get('usar_horario')), data.get('hora_inicio'), data.get('hora_fin')))
            gid = c.lastrowid
            for rid in data.get('reles'):
                c.execute("UPDATE reles SET id_grupo=? WHERE id=?", (gid, rid))

        # --- NUEVA FUNCIÓN: EDITAR GRUPO ---
        elif accion == 'editar_grupo':
            gid = data.get('id')
            c.execute("UPDATE grupos SET nombre=?, prioridad=?, usar_horario=?, hora_inicio=?, hora_fin=? WHERE id=?",
                      (data.get('nombre'), int(data.get('prioridad')), int(data.get('usar_horario')), 
                       data.get('hora_inicio'), data.get('hora_fin'), gid))
            
            # Reasignar relés: Primero liberamos todos los de este grupo
            c.execute("UPDATE reles SET id_grupo=0 WHERE id_grupo=?", (gid,))
            # Luego asignamos los nuevos seleccionados
            for rid in data.get('reles'):
                c.execute("UPDATE reles SET id_grupo=? WHERE id=?", (gid, rid))

        elif accion == 'eliminar_grupo':
            gid = data.get('id_grupo')
            c.execute("UPDATE reles SET id_grupo=0 WHERE id_grupo=?", (gid,))
            c.execute("DELETE FROM grupos WHERE id=?", (gid,))

        conn.commit()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    setup_gpio()
    sched = BackgroundScheduler()
    sched.add_job(tarea_monitoreo_energia, 'interval', minutes=1)
    sched.add_job(verificar_inteligencia, 'interval', minutes=1)
    sched.start()
    app.run(host='0.0.0.0', port=80, debug=False, use_reloader=False)