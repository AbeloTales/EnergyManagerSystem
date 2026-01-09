import sqlite3
import cv2
import pytesseract
import RPi.GPIO as GPIO
import time
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

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
    """Controla Horarios Fijos y Ciclos Intermitentes"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    now = datetime.now()
    hora_actual_str = now.strftime("%H:%M")
    
    try:
        c.execute("SELECT * FROM grupos")
        grupos = c.fetchall()
        
        for g in grupos:
            gid = g['id']
            # Obtenemos estado actual del grupo (mirando el primer relé del grupo)
            # Esto asume que todos los relés del grupo están sincronizados
            c.execute("SELECT estado FROM reles WHERE id_grupo=? LIMIT 1", (gid,))
            r_estado = c.fetchone()
            estado_actual = r_estado[0] if r_estado else 0

            nuevo_estado = None # Si se mantiene None, no hacemos cambios
            
            # --- CASO A: CONTROL POR HORARIO ---
            if g['usar_horario'] == 1:
                inicio = g['hora_inicio']
                fin = g['hora_fin']
                
                encender = False
                if inicio < fin:
                    if inicio <= hora_actual_str < fin: encender = True
                else: # Cruza medianoche
                    if hora_actual_str >= inicio or hora_actual_str < fin: encender = True
                
                nuevo_estado = 1 if encender else 0

            # --- CASO B: CONTROL CÍCLICO (Tiene prioridad sobre horario si ambos están activos) ---
            # Lógica: Si toca cambio, invertimos el estado y guardamos la hora
            if g['modo_ciclo'] == 1:
                min_on = g['ciclo_on']
                min_off = g['ciclo_off']
                last_action_str = g['ultima_accion']
                
                # Si es la primera vez (no hay fecha registrada), iniciamos encendiendo
                if not last_action_str:
                    nuevo_estado = 1
                    c.execute("UPDATE grupos SET ultima_accion=? WHERE id=?", (now.strftime("%Y-%m-%d %H:%M:%S"), gid))
                else:
                    last_action = datetime.strptime(last_action_str, "%Y-%m-%d %H:%M:%S")
                    diff_minutos = (now - last_action).total_seconds() / 60
                    
                    if estado_actual == 1: # Está ENCENDIDO
                        if diff_minutos >= min_on: # Ya cumplió su tiempo ON
                            nuevo_estado = 0 # Apagar
                            c.execute("UPDATE grupos SET ultima_accion=? WHERE id=?", (now.strftime("%Y-%m-%d %H:%M:%S"), gid))
                    else: # Está APAGADO
                        if diff_minutos >= min_off: # Ya cumplió su tiempo OFF
                            nuevo_estado = 1 # Encender
                            c.execute("UPDATE grupos SET ultima_accion=? WHERE id=?", (now.strftime("%Y-%m-%d %H:%M:%S"), gid))

            # --- APLICAR CAMBIOS ---
            # Solo si 'nuevo_estado' se definió en alguna lógica y es diferente al actual (o forzamos actualización)
            if nuevo_estado is not None:
                c.execute("UPDATE reles SET estado=? WHERE id_grupo=?", (nuevo_estado, gid))
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
                GPIO.output(r[0], True) 
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

        # CREAR (Con soporte para ciclos)
        elif accion == 'crear_grupo':
            c.execute("""INSERT INTO grupos 
                      (nombre, prioridad, usar_horario, hora_inicio, hora_fin, modo_ciclo, ciclo_on, ciclo_off) 
                      VALUES (?,?,?,?,?,?,?,?)""",
                      (data.get('nombre'), int(data.get('prioridad')), int(data.get('usar_horario')), 
                       data.get('hora_inicio'), data.get('hora_fin'),
                       int(data.get('modo_ciclo', 0)), int(data.get('ciclo_on', 0)), int(data.get('ciclo_off', 0))))
            gid = c.lastrowid
            for rid in data.get('reles'):
                c.execute("UPDATE reles SET id_grupo=? WHERE id=?", (gid, rid))

        # EDITAR (Con soporte para ciclos)
        elif accion == 'editar_grupo':
            gid = data.get('id')
            # Resetear la fecha de última acción para que el ciclo reinicie limpio
            c.execute("""UPDATE grupos SET 
                      nombre=?, prioridad=?, usar_horario=?, hora_inicio=?, hora_fin=?, 
                      modo_ciclo=?, ciclo_on=?, ciclo_off=?, ultima_accion=NULL 
                      WHERE id=?""",
                      (data.get('nombre'), int(data.get('prioridad')), int(data.get('usar_horario')), 
                       data.get('hora_inicio'), data.get('hora_fin'),
                       int(data.get('modo_ciclo', 0)), int(data.get('ciclo_on', 0)), int(data.get('ciclo_off', 0)),
                       gid))
            
            # Reasignar relés
            c.execute("UPDATE reles SET id_grupo=0 WHERE id_grupo=?", (gid,))
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