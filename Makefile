.PHONY: demo test stress

demo:
	PYTHONPATH=. python3 demo.py

test:
	PYTHONPATH=. python3 -m unittest discover -s tests -v

stress:
	PYTHONPATH=. python3 stress.py
