"""Things outside this program that a flight already involves.

Each module here talks to something the pilot is running anyway -- a flight
plan they have already built, a ground handling add-on already pushing them
back -- and turns it into facts the controller can use. Every one of them is
optional: if the other thing is not there, the module says so and the radio
works exactly as it did before.
"""
