# Dlib Face Recognition Models

## Download Models

Run the download script:
```bash
python scripts/download_dlib_models.py
```

Or manually:
```bash
cd models/dlib
curl -L -o shape_predictor_68_face_landmarks.dat.bz2 "http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2"
curl -L -o dlib_face_recognition_resnet_model_v1.dat.bz2 "http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2"
bunzip2 *.bz2
```

These files are ~100MB each and excluded from git via .gitignore.
