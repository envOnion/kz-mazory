from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth.models import User
from .models import UserProfile
from .serializers import UserProfileSerializer

def get_or_create_default_profile(user):
    profile, created = UserProfile.objects.get_or_create(
        user=user,
        defaults={
            'full_name': 'Камиль',
            'role': 'Ведущий менеджер по продажам',
            'department': 'Отдел продаж Aqua Kip',
            'email': 'kamil@aquakip.kz',
            'phone': user.username if (user.username.startswith('+') or user.username.isdigit()) else '+7 (701) 123-45-67',
            'monthly_target': 50000000.00,
            'current_sales': 31790000.00,
            'deals_count': 15,
            'rank_in_team': 1,
            'conversion_rate': 35.00,
            'avatar_url': 'https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=250&q=80'
        }
    )
    return profile

class ProfileView(APIView):
    """
    Profile management endpoint. Accessible only to authenticated users.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = get_or_create_default_profile(request.user)
        serializer = UserProfileSerializer(profile)
        return Response(serializer.data)

    def put(self, request):
        profile = get_or_create_default_profile(request.user)

        serializer = UserProfileSerializer(profile, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response({
                "status": "success",
                "message": "Профиль успешно обновлен",
                "profile": serializer.data
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
