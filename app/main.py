from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

app = FastAPI()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


class User(BaseModel):
    username: str
    email: str | None = None
    full_name: str | None = None
    disabled: bool | None = None


def fake_decode_token(token):
    return User(
        username=token + "fakedecoded", email="john@example.com", full_name="John Doe"
    )


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]):
    user = fake_decode_token(token)
    return user


@app.get("/users/me")
async def read_users_me(current_user: Annotated[User, Depends(get_current_user)]):
    return current_user


""" from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from app.pdf_operations import process_pdf, save_pdf_with_annotations
import os
import traceback

app = FastAPI()

app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def get_home():
    with open("app/static/index.html") as f:
        return HTMLResponse(content=f.read(), status_code=200)

# @app.post("/upload-pdf/")
# async def upload_pdf(file: UploadFile = File(...)):
#     try:
#         content = await file.read()
#         temp_path = f"temp/{file.filename}"
#         with open(temp_path, "wb+") as f:
#             f.write(content)
#         return {"filename": file.filename}
#     except Exception as e:
#         traceback.print_exc()
#         print(e)
#         raise HTTPException(status_code=500, detail="Error uploading PDF")
    


@app.post("/upload-pdf")
def upload_pdf(file: UploadFile = File(...)):
    # Upload a File to the server and save it in temp
    file_path = f"/temp/{file.filename}"
    print(file_path)
    try:
        with open(file.filename, 'wb+') as f:
            while contents := file.file.read(1024 * 1024):
                f.write(contents)
    except Exception:
        return {"message": "There was an error uploading the file"}
    finally:
        file.file.close()

    return {"message": f"Successfully uploaded {file.filename}"}


@app.get("/download-pdf/{filename}", response_class=FileResponse)
async def download_pdf(filename: str):
    try:
        file_path = f"temp/{filename}"
        if os.path.exists(file_path):
            return FileResponse(file_path, filename=filename)
        else:
            raise HTTPException(status_code=404, detail="PDF not found")
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error downloading PDF")

@app.post("/save-pdf/")
async def save_pdf(filename: str = Form(...), annotations: str = Form(...)):
    try:
        annotations = eval(annotations)
        save_pdf_with_annotations(filename, annotations)
        return {"message": "PDF saved successfully"}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error saving PDF")
 """