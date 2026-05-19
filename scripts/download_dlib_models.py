#!/usr/bin/env python3
"""Download dlib face recognition models to models/dlib/."""
import bz2
import os
import urllib.request

MODELS_DIR = os.path.join(os.path.dirname(__file__), "..", "models", "dlib")
os.makedirs(MODELS_DIR, exist_ok=True)

MODELS = {
    "shape_predictor_68_face_landmarks.dat":
        "http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2",
    "dlib_face_recognition_resnet_model_v1.dat":
        "http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2",
}


def main():
    for filename, url in MODELS.items():
        filepath = os.path.join(MODELS_DIR, filename)
        if os.path.exists(filepath):
            print(f"Already exists: {filename}")
            continue

        bz2_path = filepath + ".bz2"
        print(f"Downloading {filename}...")
        urllib.request.urlretrieve(url, bz2_path)

        print(f"Decompressing {filename}...")
        with bz2.open(bz2_path, "rb") as f_in:
            with open(filepath, "wb") as f_out:
                f_out.write(f_in.read())
        os.remove(bz2_path)
        print(f"Saved: {filename}")

    print("Done.")


if __name__ == "__main__":
    main()
