# Guideline formalisation demo -- Flask app + Z3 checker + terminology cache.
#
# Deliberately NOT included (see .dockerignore): the 2.3GB semantic index
# (bind.py degrades to lexical retrieval), the JVM/Node CQL toolchain, and
# the raw SNOMED/LOINC/RxNorm releases -- terms.db already holds their
# loaded contents.
#
# No ANTHROPIC_API_KEY is baked in or expected. The app reports
# server_has_api_key() == false and the UI asks a visitor to paste their
# own, so formalising a new document runs on the caller's account.
FROM python:3.14-slim

WORKDIR /app

# z3-solver, lxml, pymupdf and numpy all publish manylinux wheels, so no
# build toolchain is needed in the image.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY CQL_converter/ /app/CQL_converter/
COPY patient_checker/ /app/patient_checker/

# A container is unreachable on 127.0.0.1; bind every interface. PORT is
# what Fly injects, and api.py reads both.
ENV GF_HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
WORKDIR /app/patient_checker
CMD ["python3", "api.py"]
