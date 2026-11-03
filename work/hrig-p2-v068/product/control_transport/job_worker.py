"""Existing extension.run worker for local, job-owned native control copies."""
import sys
sys.dont_write_bytecode=True
sys.path[:0]=[parameters['new_product'],parameters['product'],parameters['scripts'],parameters['public']]
from control_transport.saved_job import execute
result=execute(parameters,job_directory)
