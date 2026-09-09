@echo off
cd /d C:\Users\YHR\npe_get_jobs
"D:\miniconda\python.exe" scripts\scheduled_pipeline.py --mode evening >> logs\pipeline_boot.log 2>&1
