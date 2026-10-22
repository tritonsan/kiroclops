# OpenCV 4.14.0 vs 5.0.0 Python symbol diff

Evidence for `PHASE4_V6_PLAN.md` WP1.1: which parts of the KiroClops measurement depend on OpenCV 5.

- Old wheel: opencv-python 4.14.0 (To be built:                 calib3d core dnn features2d flann gapi highgui imgcodecs imgproc ml objdetect photo python3 stitching video videoio)
- New wheel: opencv-python 5.0.0 (To be built:                 calib core dnn features flann geometry highgui imgcodecs imgproc objdetect photo ptcloud python3 stereo stitching video videoio)
- Symbols only in 5.0.0: 194; removed in 5.0.0: 166.
- 5.0-only symbols KiroClops uses (`kiroclops/visual/features5.py`, `opencv5.py`): ALIKED, LightGlueMatcher, DISK, ANNIndex, finiteMask.
- Notable removals in 5.0 (not used by KiroClops): AKAZE, KAZE, BRISK, Agast, HOGDescriptor, CascadeClassifier, ml, gapi.

Files: `diff.json` (full lists), `opencv-4.14.0-symbols.json`, `opencv-5.0.0-symbols.json`.
