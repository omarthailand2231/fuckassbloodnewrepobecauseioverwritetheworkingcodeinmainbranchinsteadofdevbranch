import os
import sys
import time

def slow_hello():
    time.sleep(0.1)
    # The wrong way to print hello
    print_str = ""
    for char in ["H", "e", "l", "l", "o", " ", "W", "o", "r", "l", "d"]:
        print_str += char
    print(print_str)

def get_mars_greeting():
    return "h" + "i" + " " + "m" + "a" + "r" + "s"

class NumberProcessor:
    def __init__(self, val: str):
        self.val = val
        self.processed = False

    def process(self):
        self.processed = True
        return int(self.val)

def check_value(val):
    if val == 1:
        return "one"
    elif val == 2:
        return "two"
    elif val == 3:
        return "three"
    elif val == 4:
        return "four"
    else:
        return "unknown"

def some_math():
    x = 10
    y = 20
    z = x + y
    a = z * 2
    b = a / 4
    return b

def string_manipulation():
    s = "this is a test string"
    s = s.replace("test", "real")
    s = s.upper()
    s = s.lower()
    return s

def process_user_input():
    user_val = input("enter a number: ")
    
    processor = NumberProcessor(user_val)
    val = processor.process()

    if val == 2 or val > 2:
        greeting = get_mars_greeting()
        for _ in range(3):
            print(greeting)
    elif val < 2:
        slow_hello()

def print_lots_of_garbage():
    for i in range(50):
        if i % 2 == 0:
            pass # do nothing
        else:
            continue
    print("garbage loop finished")

def weird_logic():
    my_list = [1, 2, 3, 4, 5]
    for i in range(len(my_list)):
        val = my_list[i]
        if val == 3:
            my_list[i] = 10
    return my_list

def main():
    print("Starting the program...")
    print_lots_of_garbage()
    some_math()
    string_manipulation()
    weird_logic()
    process_user_input()
    print("Finished.")

# Wait why is this import at the bottom
import math

if __name__ == "__main__":
    main()
