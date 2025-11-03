#!/usr/bin/env python3
"""
Evaluation Container for AI-based Shipping Line Tracking Assignment
Tests the automation container API and generates comprehensive reports.
"""

import requests
import time
import sys

API_URL = "http://automation:5000"
EXPECTED_VOYAGE = "YM MANDATE 0096W"
EXPECTED_DATE = "2025-02-28"
BOOKING_ID = "SINI25432400"

def wait_for_api():
    """Wait for the API to be ready"""
    print("⏳ Waiting for API to be ready...")
    for attempt in range(30):
        try:
            response = requests.get(f"{API_URL}/health", timeout=5)
            if response.status_code == 200:
                print("✅ API is ready")
                return True
        except:
            pass
        time.sleep(2)
    print("❌ API not ready after 30 attempts")
    return False

def test_fresh_request():
    """Test fresh tracking request"""
    print("\n🚢 Testing Fresh Request...")
    start_time = time.time()
    
    try:
        response = requests.post(f"{API_URL}/track", 
                               json={"booking_id": BOOKING_ID}, 
                               timeout=1000)  # 5 minutes for automation to complete
        
        if response.status_code != 200:
            print(f"❌ Failed: HTTP {response.status_code}")
            return False
            
        data = response.json()
        execution_time = time.time() - start_time
        
        # Verify correct data
        if data.get("voyage_number") != EXPECTED_VOYAGE:
            print(f"❌ Wrong voyage: got '{data.get('voyage_number')}', expected '{EXPECTED_VOYAGE}'")
            return False
            
        if data.get("arrival_date") != EXPECTED_DATE:
            print(f"❌ Wrong date: got '{data.get('arrival_date')}', expected '{EXPECTED_DATE}'")
            return False
            
        if data.get("used_cache") != False:
            print(f"❌ Should not use cache on fresh request")
            return False
            
        print(f"✅ Fresh request passed ({execution_time:.1f}s)")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def test_cached_request():
    """Test cached request request"""
    print("\n💾 Testing Cached Request...")
    start_time = time.time()
    
    try:
        response = requests.post(f"{API_URL}/track", 
                               json={"booking_id": BOOKING_ID}, 
                               timeout=1000)  # 5 minutes (should be faster if cached)
        
        if response.status_code != 200:
            print(f"❌ Failed: HTTP {response.status_code}")
            return False
            
        data = response.json()
        execution_time = time.time() - start_time
        
        # Verify correct data
        if data.get("voyage_number") != EXPECTED_VOYAGE:
            print(f"❌ Wrong voyage: got '{data.get('voyage_number')}', expected '{EXPECTED_VOYAGE}'")
            return False
            
        if data.get("arrival_date") != EXPECTED_DATE:
            print(f"❌ Wrong date: got '{data.get('arrival_date')}', expected '{EXPECTED_DATE}'")
            return False
            
        if data.get("used_cache") != True:
            print(f"❌ Should use cache on repeat request")
            return False
            
        print(f"✅ Cached request passed ({execution_time:.1f}s)")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def main():
    print("🧪 Starting Simple Evaluation")
    print("=" * 40)
    
    if not wait_for_api():
        sys.exit(1)
    
    tests_passed = 0
    total_tests = 2
    
    # Test fresh request
    if test_fresh_request():
        tests_passed += 1
    
    # Test cached request  
    if test_cached_request():
        tests_passed += 1
    
    print(f"\n📊 Results: {tests_passed}/{total_tests} tests passed")
    
    if tests_passed == total_tests:
        print("🎉 All tests passed!")
        sys.exit(0)
    else:
        print("❌ Some tests failed!")
        sys.exit(1)

if __name__ == "__main__":
    main() 