FROM public.ecr.aws/lambda/python:3.11

# The Terraform bootstrap and GitHub Actions deployment both build this image.
# The public Lambda base image pins the requested Python 3.11 runtime.
RUN pip install --no-cache-dir joblib pandas scikit-learn xgboost

COPY lambda_handler.py ${LAMBDA_TASK_ROOT}/lambda_handler.py

CMD ["lambda_handler.lambda_handler"]
