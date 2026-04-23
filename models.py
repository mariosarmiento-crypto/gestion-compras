from fastapi import FastAPI, Depends, HTTPException, status, Response, Query, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date as date_type
from io import BytesIO
import pandas as pd
from . import models, schemas, auth, database, export_utils
from fastapi.staticfiles import StaticFiles
from .database import engine, get_db

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Sistema de Gestión de Compras Hoteles")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir Frontend
@app.get("/")
async def read_index():
    from fastapi.responses import FileResponse
    import os
    return FileResponse(os.path.join("frontend", "index.html"))


# Inicialización de datos (Hoteles por defecto y Admin si no existe)
@app.on_event("startup")
def startup_populate():
    db = database.SessionLocal()
    # Hoteles
    for hotel_name in ["Mare Hotel", "Reñaca House"]:
        if not db.query(models.Hotel).filter(models.Hotel.name == hotel_name).first():
            db.add(models.Hotel(name=hotel_name))
    # Admin
    if not db.query(models.User).filter(models.User.username == "admin").first():
        hashed_pw = auth.get_password_hash("admin123")
        db.add(models.User(username="admin", hashed_password=hashed_pw, role="admin"))
    db.commit()
    db.close()

# --- AUTH ---
@app.post("/token", response_model=schemas.Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = auth.create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/users/me", response_model=schemas.User)
async def read_users_me(current_user: models.User = Depends(auth.get_current_user)):
    return current_user

@app.patch("/users/me/update")
async def update_user_me(
    username: Optional[str] = None, 
    password: Optional[str] = None, 
    db: Session = Depends(get_db), 
    current_user: models.User = Depends(auth.get_current_user)
):
    if username:
        # Verificar si el nombre de usuario ya existe
        existing = db.query(models.User).filter(models.User.username == username, models.User.id != current_user.id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Este nombre de usuario ya está en uso")
        current_user.username = username
    
    if password:
        current_user.hashed_password = auth.get_password_hash(password)
    
    db.commit()
    return {"message": "Datos de usuario actualizados correctamente"}

# --- HOTELES ---
@app.get("/hoteles", response_model=List[schemas.Hotel])
def get_hoteles(db: Session = Depends(get_db)):
    return db.query(models.Hotel).all()

# --- PROVEEDORES ---
@app.get("/proveedores", response_model=List[schemas.Provider])
def get_proveedores(q: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(models.Provider)
    if q:
        query = query.filter(models.Provider.name.contains(q))
    return query.all()

@app.get("/cuentas")
def get_cuentas(db: Session = Depends(get_db)):
    # Obtener nombres únicos de cuentas desde la tabla de movimientos
    accounts = db.query(models.Movement.account_name).distinct().all()
    return [a[0] for a in accounts if a[0]]

# --- MOVIMIENTOS ---
@app.post("/movimientos", response_model=schemas.Movement)
def create_movement(movement: schemas.MovementCreate, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    # Manejar Proveedor (Crear si no existe o validar RUT)
    provider = db.query(models.Provider).filter(models.Provider.name == movement.provider_name).first()
    if provider:
        if provider.rut != movement.provider_rut:
            raise HTTPException(status_code=400, detail=f"El proveedor {movement.provider_name} ya existe con otro RUT ({provider.rut})")
    else:
        # Verificar si el RUT ya está en uso por otro proveedor
        provider_by_rut = db.query(models.Provider).filter(models.Provider.rut == movement.provider_rut).first()
        if provider_by_rut:
            raise HTTPException(status_code=400, detail=f"El RUT {movement.provider_rut} ya está asociado al proveedor {provider_by_rut.name}")
        
        provider = models.Provider(name=movement.provider_name, rut=movement.provider_rut)
        db.add(provider)
        db.commit()
        db.refresh(provider)

    # Crear Movimiento
    db_movement = models.Movement(
        hotel_id=movement.hotel_id,
        date=movement.date,
        description=movement.description,
        account_name=movement.account_name,
        provider_id=provider.id,
        doc_type=movement.doc_type,
        doc_number=movement.doc_number,
        total=movement.total,
        iva=movement.iva,
        net=movement.net,
        observations=movement.observations,
        user_id=current_user.id
    )
    try:
        db.add(db_movement)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="Registro duplicado detectado (Proveedor + Tipo Doc + Nº Doc)")
    
    db.refresh(db_movement)
    return db_movement

@app.get("/movimientos", response_model=List[schemas.Movement])
def get_movements(
    hotel_id: Optional[int] = None,
    date_from: Optional[date_type] = None,
    date_to: Optional[date_type] = None,
    provider_id: Optional[int] = None,
    doc_number: Optional[str] = None,
    only_mine: Optional[bool] = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    query = db.query(models.Movement)
    if only_mine:
        query = query.filter(models.Movement.user_id == current_user.id)
    if hotel_id:
        query = query.filter(models.Movement.hotel_id == hotel_id)
    if date_from:
        query = query.filter(models.Movement.date >= date_from)
    if date_to:
        query = query.filter(models.Movement.date <= date_to)
    if provider_id:
        query = query.filter(models.Movement.provider_id == provider_id)
    if doc_number:
        query = query.filter(models.Movement.doc_number.contains(doc_number))
    return query.all()

@app.delete("/movimientos/{movement_id}")
def delete_movement(movement_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    db_movement = db.query(models.Movement).filter(models.Movement.id == movement_id).first()
    if not db_movement:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado")
    
    # Solo el admin o el dueño pueden borrar
    if current_user.role != 'admin' and db_movement.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="No tiene permiso para eliminar este registro")
        
    db.delete(db_movement)
    db.commit()
    return {"message": "Movimiento eliminado"}

@app.delete("/movimientos/clear/all")
def clear_all_movements(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Solo el administrador puede realizar esta acción")
    db.query(models.Movement).delete()
    db.commit()
    return {"message": "Todos los registros han sido eliminados"}

# --- EXPORT ---
@app.get("/export/{format}")
def export_movements(
    format: str,
    hotel_id: Optional[int] = None,
    date_from: Optional[date_type] = None,
    date_to: Optional[date_type] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    movements = get_movements(hotel_id, date_from, date_to, None, None, db, current_user)
    data = []
    for m in movements:
        data.append({
            "Hotel": m.hotel.name,
            "Fecha": m.date,
            "Descripción": m.description,
            "Cuenta": m.account_name,
            "Proveedor": m.provider.name,
            "RUT": m.provider.rut,
            "Tipo Doc": m.doc_type,
            "Nº Doc": m.doc_number,
            "Total": m.total,
            "IVA": m.iva,
            "Neto": m.net,
            "Observaciones": m.observations
        })
    
    if format == "excel":
        file = export_utils.generate_excel(data)
        return Response(content=file.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=reporte.xlsx"})
    elif format == "pdf":
        file = export_utils.generate_pdf(data)
        return Response(content=file.getvalue(), media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=reporte.pdf"})
    
    raise HTTPException(status_code=400, detail="Formato no soportado")

@app.post("/import/excel")
async def import_excel(
    file: UploadFile = File(...), 
    db: Session = Depends(get_db), 
    current_user: models.User = Depends(auth.get_current_user)
):
    try:
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error al leer el archivo Excel: {str(e)}")

    column_mapping = {
        'Fecha': 'date', 'Hotel': 'hotel_name', 'Descripción': 'description',
        'Cuenta': 'account_name', 'Proveedor': 'provider_name', 'RUT': 'provider_rut',
        'Tipo Doc': 'doc_type', 'Número': 'doc_number', 'Nº Doc': 'doc_number',
        'Total': 'total', 'IVA': 'iva', 'Neto': 'net', 'Observaciones': 'observations'
    }
    df = df.rename(columns=lambda x: column_mapping.get(x.strip(), x.strip()) if isinstance(x, str) else x)

    required = ['date', 'hotel_name', 'provider_name', 'provider_rut', 'total']
    for col in required:
        if col not in df.columns:
            raise HTTPException(status_code=400, detail=f"Falta columna requerida: {col}. Use el formato del reporte.")

    hoteles_db = {h.name.lower(): h.id for h in db.query(models.Hotel).all()}
    imported, errors = 0, []

    for index, row in df.iterrows():
        try:
            h_id = hoteles_db.get(str(row['hotel_name']).strip().lower())
            if not h_id: continue
            
            p_name, p_rut = str(row['provider_name']).strip(), str(row['provider_rut']).strip()
            provider = db.query(models.Provider).filter(models.Provider.rut == p_rut).first()
            if not provider:
                provider = models.Provider(name=p_name, rut=p_rut)
                db.add(provider); db.flush()

            m_date = pd.to_datetime(row['date']).date()
            movement = models.Movement(
                hotel_id=h_id, date=m_date, description=str(row.get('description', '')),
                account_name=str(row.get('account_name', 'Sin clasificar')),
                provider_id=provider.id, doc_type=str(row.get('doc_type', 'Otro')),
                doc_number=str(row.get('doc_number', 'S/N')),
                total=float(row.get('total', 0)), iva=float(row.get('iva', 0)),
                net=float(row.get('net', 0)), observations=str(row.get('observations', '')),
                user_id=current_user.id
            )
            db.merge(movement) # Usar merge por si hay duplicados ignorar? No, mejor add
            imported += 1
        except Exception as e:
            errors.append(f"Fila {index+2}: {str(e)}")

    db.commit()
    return {"message": f"Importación finalizada. {imported} registros cargados.", "errors": errors}

app.mount("/", StaticFiles(directory="frontend"), name="frontend")
