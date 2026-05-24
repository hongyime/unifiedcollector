#!/bin/sh
set -e

mkdir -p /data/models
cd /data/models

if [ ! -f "shape_predictor_68_face_landmarks.dat" ]; then
    echo "Downloading shape_predictor_68_face_landmarks.dat..."
    wget -q --show-progress -O shape_predictor_68_face_landmarks.dat.bz2 \
        http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2
    bunzip2 shape_predictor_68_face_landmarks.dat.bz2
    echo "shape_predictor downloaded."
else
    echo "shape_predictor already present."
fi

if [ ! -f "dlib_face_recognition_resnet_model_v1.dat" ]; then
    echo "Downloading dlib_face_recognition_resnet_model_v1.dat..."
    wget -q --show-progress -O dlib_face_recognition_resnet_model_v1.dat.bz2 \
        http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2
    bunzip2 dlib_face_recognition_resnet_model_v1.dat.bz2
    echo "resnet model downloaded."
else
    echo "resnet model already present."
fi

echo "dlib models ready."
